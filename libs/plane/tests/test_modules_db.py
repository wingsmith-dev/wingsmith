"""Module services against a real Postgres, because a compiled statement cannot expire a column.

Every other module suite builds statements and schemas without a database. That is fast and it is
blind to the one thing a write path can get wrong on its own: `updated_at` carries a server-side
`onupdate`, so SQLAlchemy expires it after an UPDATE and re-reads it lazily — which in async code
is a `MissingGreenlet` at response-render time, long after the write itself succeeded. Nothing
short of executing the UPDATE and then reading the schema can see that.

So each module here runs `create → get → list → patch → delete` through its service against a
migrated database and asserts the returned schema is whole, with `updated_at` and `revision`
checked after the patch. The suite is marked `database` and deselected by default (`addopts`);
with no reachable server every test skips rather than fails.
"""

import os
import subprocess

from pathlib import Path

import bcrypt
import pytest
import psycopg

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth import ENROLMENT_PREFIX
from app.auth import AuthManager
from app.core.ids import NODE
from app.core.ids import TENANT
from app.core.ids import new_uid
from app.db.notify import MemoryNotifier
from app.hub.server import _resolve_connection
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.ingestion.desired import desired_frames
from app.ingestion.desired import desired_object
from app.ingestion.desired import stream_desired
from app.ingestion.operations import open_operations
from app.ingestion.operations import operation_desired
from app.modules.pools.schemas import PoolCreateRequest
from app.modules.pools.schemas import PoolUpdateRequest
from app.modules.pools.services import PoolService
from app.modules.quotas.schemas import QuotaCreateRequest
from app.modules.quotas.schemas import QuotaUpdateRequest
from app.modules.events.services import EventService
from app.modules.quotas.services import QuotaService
from app.modules.tenants.schemas import TenantCreateRequest
from app.modules.tenants.schemas import TenantUpdateRequest
from app.modules.clusters.schemas import ClusterCreateRequest
from app.modules.clusters.schemas import ClusterUpdateRequest
from app.modules.clusters.schemas import ClusterServingRequest
from app.modules.policies.schemas import PolicyCreateRequest
from app.modules.policies.schemas import PolicyUpdateRequest
from app.modules.policies.schemas import PolicyAttachmentRequest
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.projects.schemas import ProjectUpdateRequest
from app.modules.tenants.services import TenantService
from app.modules.artifacts.schemas import ArtifactCreateRequest
from app.modules.artifacts.schemas import ArtifactUpdateRequest
from app.modules.artifacts.schemas import ArtifactLocationRequest
from app.modules.clusters.services import ClusterService
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.endpoints.schemas import EndpointUpdateRequest
from app.modules.policies.services import PolicyService
from app.modules.projects.services import ProjectService
from app.modules.workloads.schemas import CacheSchema
from app.modules.workloads.schemas import ShapeSchema
from app.modules.workloads.schemas import OverlaySchema
from app.modules.workloads.schemas import WorkloadCreateRequest
from app.modules.workloads.schemas import WorkloadServingUpdate
from app.modules.workloads.schemas import WorkloadUpdateRequest
from app.modules.workloads.schemas import WorkloadServingRequest
from app.modules.artifacts.services import ArtifactService
from app.modules.components.schemas import ComponentCreateRequest
from app.modules.components.schemas import ComponentUpdateRequest
from app.modules.endpoints.services import EndpointService
from app.modules.operations.schemas import OperationCreateRequest
from app.modules.workloads.services import WorkloadService
from app.modules.components.services import ComponentService
from app.modules.deployments.schemas import DeploymentUpdateRequest
from app.modules.deployments.schemas import DeploymentAuthorityRequest
from app.modules.operations.services import TERMINAL_PHASES
from app.modules.operations.services import OperationService
from app.modules.deployments.services import DeploymentService
from app.modules.priority_classes.schemas import PriorityClassCreateRequest
from app.modules.priority_classes.schemas import PriorityClassUpdateRequest
from app.modules.priority_classes.services import PriorityClassService


pytestmark = pytest.mark.database


ROOT = Path(__file__).resolve().parents[1]

ADMIN_URL = os.environ.get("HANGAR_TEST_DATABASE_URL", "postgresql+psycopg://hangar:hangar@localhost:5442/hangar")
TEST_DATABASE = "hangar_modules_test"


# --------------------------------------------------------------------------- database


@pytest.fixture(scope="session")
def database() -> str:
    """A freshly migrated test database, or a skip when nothing is listening."""
    admin = make_url(ADMIN_URL)
    dsn = admin.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        connection = psycopg.connect(dsn, connect_timeout=3, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"no database at {admin.host}:{admin.port} — run `just dev-up` ({exc})")

    with connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{TEST_DATABASE}"')

    url = admin.set(database=TEST_DATABASE).render_as_string(hide_password=False)
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={**os.environ, "HANGAR_DATABASE_URL": url},
    )
    return url


@pytest.fixture
async def db(database: str):
    """One empty database and one session per test, with the app's own session settings."""
    engine = create_async_engine(database, poolclass=NullPool)
    async with engine.begin() as connection:
        tables = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
        )
        names = ", ".join(f'"{row[0]}"' for row in tables)
        if names:
            await connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            yield session
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- seeds


async def seed_tenant(db: AsyncSession, slug: str = "acme") -> str:
    uid = new_uid(TENANT)
    await db.execute(
        text("INSERT INTO bind_tenant (uid, slug, name) VALUES (:uid, :slug, :name)"),
        {"uid": uid, "slug": slug, "name": slug.title()},
    )
    await db.commit()
    return uid


async def seed_node(db: AsyncSession, cluster_uid: str, name: str = "node-a") -> str:
    uid = new_uid(NODE)
    await db.execute(
        text(
            "INSERT INTO supply_node (uid, cluster_id, name)"
            " VALUES (:uid, (SELECT id FROM supply_cluster WHERE uid = :cluster), :name)"
        ),
        {"uid": uid, "cluster": cluster_uid, "name": name},
    )
    await db.commit()
    return uid


async def a_cluster(db: AsyncSession, name: str = "alpha", phase: str = "active"):
    """A cluster in service, because `pending` is not allowed to carry traffic.

    The registry default is `pending` and the resolution honours it, so a seed that
    took the default would front an endpoint with a cluster nobody has put into
    service and read as a broken routing table rather than as an unfinished one.
    """
    return await ClusterService(db).create(ClusterCreateRequest(name=name, region="eu-west-1", phase=phase))


async def a_project(db: AsyncSession, slug: str = "search"):
    await seed_tenant(db)
    return await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug=slug, name=slug.title()))


async def an_artifact(db: AsyncSession, digest: str = "sha256:aaaa", name: str = "llama"):
    return await ArtifactService(db).create(ArtifactCreateRequest(digest=digest, name=name))


def a_serving(artifact_ref: str, **extra) -> WorkloadServingRequest:
    """The serving block every fixture workload is created with.

    Stated inline rather than named, because there is no shared object to name any
    more: two workloads built from this helper carry the same `serving_digest`
    without either of them having to find the other first.
    """
    fields = {
        "artifact": artifact_ref,
        "engine": "vllm",
        "engine_runtime_version": "0.6.0",
        "context_length": 8192,
        "shape": ShapeSchema(device_class="h100"),
    }
    return WorkloadServingRequest(**(fields | extra))


async def an_environment(db: AsyncSession):
    """One cluster, one project, one artifact and the default priority class.

    Separated from `a_workload` because several tests need a second workload beside
    the first, and every one of these seeds is unique by name — calling the whole
    fixture twice is a conflict rather than a second workload.
    """
    cluster = await a_cluster(db)
    project = await a_project(db)
    artifact = await an_artifact(db)
    await PriorityClassService(db).create(
        PriorityClassCreateRequest(name="standard", value=100, is_default=True),
    )
    return cluster, project, artifact


async def a_workload(db: AsyncSession, name: str = "llama-prod", serving=None, **extra):
    """A workload pinned to one cluster, which is the shape every deployment test wants.

    Pinning is placement's declared escape hatch, and it is what these fixtures
    need: there is no supply seeded here, so an unpinned workload would expand to
    nothing and there would be no per-cluster copy to assert on.
    """
    cluster, project, artifact = await an_environment(db)
    workload = await WorkloadService(db).create(
        WorkloadCreateRequest(
            cluster=cluster.id,
            serving=serving or a_serving(artifact.id),
            name=name,
            project=project.id,
            **extra,
        )
    )
    return cluster, project, artifact, workload


async def a_deployment(db: AsyncSession, name: str = "llama-prod", **extra):
    """The one per-cluster copy a pinned workload expands into."""
    cluster, project, artifact, workload = await a_workload(db, name=name, **extra)
    deployment = await DeploymentService(db).get(workload.deployments[0].id)
    return cluster, project, artifact, deployment


def assert_patched(before, after) -> None:
    """The exact shape that was raising: a patched row renders with a moved stamp.

    `updated_at` is null until the first update and `revision` is derived from it,
    so a service that never re-read the column after committing would either raise
    or hand back the revision it was given.
    """
    assert after.updated_at is not None
    assert before.updated_at is None or after.updated_at >= before.updated_at
    assert after.revision != before.revision
    assert after.revision == str(int(after.updated_at.timestamp() * 1_000_000))


# --------------------------------------------------------------------------- modules


async def test_clusters_lifecycle(db: AsyncSession) -> None:
    service = ClusterService(db)
    created = await service.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    assert created.id.startswith("clu_")
    assert created.created_at is not None

    assert (await service.get(created.id)).id == created.id
    listed = await service.list()
    assert [item.id for item in listed.items] == [created.id]

    patched = await service.update(created.id, ClusterUpdateRequest(phase="active"))
    assert patched.phase == "active"
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_a_registered_cluster_is_labelled_for_the_metrics_store_by_its_own_name(db: AsyncSession) -> None:
    """The chart stamps `external_labels.cluster` from the same name (ADR 0011)."""
    service = ClusterService(db)
    created = await service.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    assert created.metrics_label == "alpha"

    corrected = await service.update(created.id, ClusterUpdateRequest(metrics_label="alpha-prod"))
    assert corrected.metrics_label == "alpha-prod"

    cleared = await service.update(created.id, ClusterUpdateRequest(metrics_label=None))
    assert cleared.metrics_label is None


async def test_a_cluster_registered_with_no_metrics_label_keeps_none(db: AsyncSession) -> None:
    """An explicit null is how a cluster whose store label is unknown stays honest."""
    service = ClusterService(db)
    created = await service.create(ClusterCreateRequest(name="alpha", region="eu-west-1", metrics_label=None))
    assert created.metrics_label is None

    blank = await service.create(ClusterCreateRequest(name="beta", region="eu-west-1", metrics_label=""))
    assert blank.metrics_label is None


async def test_enrolment_token_is_minted_once_rotated_and_resolvable(db: AsyncSession) -> None:
    service = ClusterService(db)
    created = await service.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))

    setup = await service.setup_instructions(created.id, host="hangar.example")
    assert setup.enrolment_token is None
    assert setup.enrolment_token_set is False

    issued = await service.issue_enrolment_token(created.id, host="hangar.example")
    assert issued.enrolment_token is not None
    assert issued.enrolment_token_set is True
    assert f"value: {issued.enrolment_token}" in issued.values_yaml

    resolved = await service.repo.by_token_hash(AuthManager.hash_enrolment_token(issued.enrolment_token))
    assert resolved is not None and resolved.uid == created.id

    rotated = await service.issue_enrolment_token(created.id, host="hangar.example")
    assert rotated.enrolment_token != issued.enrolment_token
    assert await service.repo.by_token_hash(AuthManager.hash_enrolment_token(issued.enrolment_token)) is None
    assert (await service.repo.by_token_hash(AuthManager.hash_enrolment_token(rotated.enrolment_token))).uid == created.id

    later = await service.setup_instructions(created.id, host="hangar.example")
    assert later.enrolment_token is None
    assert later.enrolment_token_set is True


async def test_enrolment_token_also_stores_a_bcrypt_verifier_and_rotation_replaces_both(
    db: AsyncSession,
) -> None:
    """One credential, two hashes: the hub's sha256 and the write ingress's bcrypt.

    Rotation must overwrite both or the two drift apart, and a cluster ends up
    authenticating to the hub with one token while pushing telemetry with another.
    """
    service = ClusterService(db)
    created = await service.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))

    row = await service.repo.get(created.id)
    assert row.remote_write_hash is None

    issued = await service.issue_enrolment_token(created.id, host="hangar.example")
    row = await service.repo.get(created.id)
    assert row.remote_write_hash is not None
    assert bcrypt.checkpw(issued.enrolment_token.encode(), row.remote_write_hash.encode())
    first = row.remote_write_hash

    rotated = await service.issue_enrolment_token(created.id, host="hangar.example")
    row = await service.repo.get(created.id)
    assert row.remote_write_hash != first
    assert bcrypt.checkpw(rotated.enrolment_token.encode(), row.remote_write_hash.encode())
    assert not bcrypt.checkpw(issued.enrolment_token.encode(), row.remote_write_hash.encode())
    assert row.token_hash == AuthManager.hash_enrolment_token(rotated.enrolment_token)

    assert row.remote_write_hash not in rotated.values_yaml


async def test_the_hub_resolves_an_enrolment_token_to_its_cluster(db: AsyncSession, database: str, monkeypatch) -> None:
    """Covers `_resolve_connection`, which opens a session of its own.

    Every other test of this path goes through the repository with a session
    handed to it, so none of them exercised the one caller that has to build one
    — and it shipped calling the sessionmaker a level short, closing every
    operator's handshake with an internal error. Going through the hub entry
    point rather than the repository is the whole point of this test.
    """
    service = ClusterService(db)
    created = await service.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    issued = await service.issue_enrolment_token(created.id, host="hangar.example")
    assert issued.enrolment_token is not None

    engine = create_async_engine(database, poolclass=NullPool)
    monkeypatch.setattr("app.hub.server.session_factory", lambda: async_sessionmaker(engine, expire_on_commit=False))
    try:
        principal = await _resolve_connection(issued.enrolment_token)
        assert principal is not None
        assert principal.scope == created.id
        assert principal.name == "alpha"

        assert await _resolve_connection(f"{ENROLMENT_PREFIX}.resolves-to-no-cluster") is None
    finally:
        await engine.dispose()


async def test_cluster_serving_is_set_replaced_and_cleared(db: AsyncSession) -> None:
    service = ClusterService(db)
    created = await service.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    assert created.serving is None

    set_once = await service.set_serving(created.id, ClusterServingRequest(url="http://dynamo.alpha.svc:8000"))
    assert set_once.serving is not None
    assert set_once.serving.url == "http://dynamo.alpha.svc:8000"
    assert set_once.serving.credential_id is None
    assert set_once.serving.enabled is True

    replaced = await service.set_serving(
        created.id, ClusterServingRequest(url="https://alpha.example.com", enabled=False)
    )
    assert replaced.serving.url == "https://alpha.example.com"
    assert replaced.serving.enabled is False
    assert (await service.get(created.id)).serving.url == "https://alpha.example.com"

    with pytest.raises(Invalid):
        await service.set_serving(created.id, ClusterServingRequest(url="   "))
    with pytest.raises(Invalid):
        await service.set_serving(
            created.id, ClusterServingRequest(url="https://alpha.example.com", credential_id="not-a-uuid")
        )
    with pytest.raises(Invalid):
        await service.set_serving(
            created.id,
            ClusterServingRequest(
                url="https://alpha.example.com", credential_id="00000000-0000-0000-0000-000000000000"
            ),
        )

    await service.clear_serving(created.id)
    assert (await service.get(created.id)).serving is None

    with pytest.raises(NotFound):
        await service.set_serving("clu_missing", ClusterServingRequest(url="https://nowhere.example.com"))


async def test_tenants_lifecycle(db: AsyncSession) -> None:
    service = TenantService(db)
    created = await service.create(TenantCreateRequest(slug="acme", name="Acme"))

    assert (await service.get("acme")).id == created.id
    assert (await service.list()).count == 1

    patched = await service.update(created.id, TenantUpdateRequest(name="Acme Inc"))
    assert patched.name == "Acme Inc"
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_a_tenant_holding_projects_refuses_to_be_deleted(db: AsyncSession) -> None:
    tenant = await TenantService(db).create(TenantCreateRequest(slug="acme", name="Acme"))
    await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug="search", name="Search"))

    assert (await TenantService(db).get(tenant.id)).project_count == 1
    with pytest.raises(Conflict):
        await TenantService(db).delete(tenant.id)


async def test_projects_lifecycle(db: AsyncSession) -> None:
    await seed_tenant(db)
    service = ProjectService(db)
    created = await service.create(ProjectCreateRequest(tenant="acme", slug="search", name="Search"))
    assert created.tenant.slug == "acme"

    assert (await service.get(created.id)).slug == "search"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, ProjectUpdateRequest(name="Search Team"))
    assert patched.name == "Search Team"
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_priority_classes_lifecycle(db: AsyncSession) -> None:
    service = PriorityClassService(db)
    created = await service.create(PriorityClassCreateRequest(name="batch", value=10))

    assert (await service.get(created.id)).name == "batch"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, PriorityClassUpdateRequest(value=20))
    assert patched.value == 20
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_the_default_priority_class_cannot_be_deleted_out_from_under_the_plane(
    db: AsyncSession,
) -> None:
    service = PriorityClassService(db)
    standard = await service.create(PriorityClassCreateRequest(name="standard", value=100, is_default=True))

    with pytest.raises(Conflict):
        await service.delete(standard.id)

    batch = await service.create(PriorityClassCreateRequest(name="batch", value=10, is_default=True))
    await service.delete(standard.id)
    assert (await service.get(batch.id)).is_default is True


async def test_artifacts_lifecycle(db: AsyncSession) -> None:
    service = ArtifactService(db)
    created = await service.create(
        ArtifactCreateRequest(
            digest="sha256:aaaa",
            name="llama",
            locations=[ArtifactLocationRequest(scheme="s3", uri="s3://weights/llama")],
        )
    )
    assert [loc.uri for loc in created.locations] == ["s3://weights/llama"]

    assert (await service.get(created.id)).digest == "sha256:aaaa"
    assert (await service.list()).count == 1

    patched = await service.update(
        created.id,
        ArtifactUpdateRequest(
            name="llama-3",
            locations=[ArtifactLocationRequest(scheme="s3", uri="s3://weights/llama-3")],
        ),
    )
    assert patched.name == "llama-3"
    assert [loc.uri for loc in patched.locations] == ["s3://weights/llama-3"]

    kept = await service.update(
        created.id,
        ArtifactUpdateRequest(
            locations=[ArtifactLocationRequest(scheme="s3", uri="s3://weights/llama-3", region="eu-west-1")]
        ),
    )
    assert [(loc.uri, loc.region) for loc in kept.locations] == [("s3://weights/llama-3", "eu-west-1")]
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_the_serving_configuration_is_created_read_and_patched(db: AsyncSession) -> None:
    """The lifecycle the recipe used to have, on the object that absorbed it.

    A serving configuration is no longer created, listed and deleted on its own: it
    is written with the workload, read back on it, and patched through it. What has
    to survive the move is that a patch reaches the stored columns and moves the
    stamp the next conditional update is checked against.
    """
    _, _, artifact, created = await a_workload(db)
    service = WorkloadService(db)
    assert created.serving.artifact_id == artifact.id
    assert created.serving.engine == "vllm"
    assert created.serving_digest.startswith("svg_")

    assert (await service.get(created.id)).serving.engine == "vllm"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(engine_runtime_version="0.7.0")))
    assert patched.serving.engine_runtime_version == "0.7.0"
    assert patched.serving_digest != created.serving_digest
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_the_same_configuration_written_twice_carries_one_digest(db: AsyncSession) -> None:
    """The whole mechanism the recipe's sharing was for, and the reason it is gone.

    The console used to compare eight fields in the browser to decide whether an
    existing recipe could be reused. Identity is the content: two workloads asking
    for the same engine, weights and shape land on the same digest without either of
    them having to find the other, and one field changing is a different digest.
    """
    cluster, project, artifact = await an_environment(db)
    service = WorkloadService(db)
    one = await service.create(
        WorkloadCreateRequest(cluster=cluster.id, serving=a_serving(artifact.id), name="chat-a", project=project.id)
    )
    same = await service.create(
        WorkloadCreateRequest(cluster=cluster.id, serving=a_serving(artifact.id), name="chat-b", project=project.id)
    )
    other = await service.create(
        WorkloadCreateRequest(
            cluster=cluster.id,
            serving=a_serving(artifact.id, context_length=4096),
            name="chat-c",
            project=project.id,
        )
    )

    assert one.serving_digest == same.serving_digest
    assert other.serving_digest != one.serving_digest
    assert [w.name for w in (await service.list(serving_digest=one.serving_digest)).items] == ["chat-a", "chat-b"]


async def test_a_reloaded_workload_hashes_to_the_digest_it_was_created_with(db: AsyncSession) -> None:
    """A create and the next read of the same row must not be two configurations.

    `kv_cache_fraction` is `Numeric(5, 4)`, so a Python float written straight
    through comes back as `Decimal("0.9000")`, and the digest hashes the column as
    the string it round-trips as. Without quantising on the way in, the digest a
    create returned and the digest the next update computed would differ for a row
    nobody touched — one configuration with two identities, produced by a reload.
    """
    cluster, project, artifact = await an_environment(db)
    service = WorkloadService(db)
    created = await service.create(
        WorkloadCreateRequest(
            cluster=cluster.id,
            serving=a_serving(artifact.id, cache=CacheSchema(kv_cache_fraction=0.9)),
            name="fractional",
            project=project.id,
        )
    )

    reloaded = await service.get(created.id)
    assert reloaded.serving_digest == created.serving_digest
    assert reloaded.serving.cache.kv_cache_fraction == 0.9

    untouched = await service.update(created.id, WorkloadUpdateRequest(labels={"tier": "gold"}))
    assert untouched.serving_digest == created.serving_digest


async def test_a_workload_carries_an_overlay_the_plane_never_reads_inside(db: AsyncSession) -> None:
    """Opaque both ways: what goes in comes back out, and a patch replaces it whole."""
    cluster, project, artifact = await an_environment(db)
    service = WorkloadService(db)
    pod_spec = {"containers": [{"name": "engine", "env": [{"name": "NCCL_DEBUG", "value": "INFO"}]}]}
    created = await service.create(
        WorkloadCreateRequest(
            cluster=cluster.id,
            serving=a_serving(artifact.id, overlay=OverlaySchema(pod_spec=pod_spec)),
            name="llama-overlaid",
            project=project.id,
        )
    )
    assert created.serving.overlay.pod_spec == pod_spec
    assert created.serving.overlay.dynamo == {}

    patched = await service.update(
        created.id,
        WorkloadUpdateRequest(serving=WorkloadServingUpdate(overlay=OverlaySchema(dynamo={"replicas": 2}))),
    )
    assert patched.serving.overlay.dynamo == {"replicas": 2}
    assert patched.serving.overlay.pod_spec == {}

    plain = await service.create(
        WorkloadCreateRequest(
            cluster=cluster.id, serving=a_serving(artifact.id), name="llama-plain", project=project.id
        )
    )
    assert (plain.serving.overlay.pod_spec, plain.serving.overlay.dynamo) == ({}, {})


async def test_bumping_the_runtime_version_moves_the_derived_image(db: AsyncSession) -> None:
    """A version bump used to be stored and then silently ignored.

    `create` resolves an image and persists it; the update then passed that stored
    value back as the declared one, which always wins. So the workload reported the
    new engine version while every cluster went on pulling the old image.
    """
    _, _, _, workload = await a_workload(db)
    assert workload.serving.engine_runtime_image == "nvcr.io/nvidia/ai-dynamo/vllm-runtime:0.6.0"

    patched = await WorkloadService(db).update(
        workload.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(engine_runtime_version="1.4.2"))
    )
    assert patched.serving.engine_runtime_version == "1.4.2"
    assert patched.serving.engine_runtime_image == "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.2"


async def test_a_hand_pinned_runtime_image_survives_a_version_bump(db: AsyncSession) -> None:
    """The override is the whole reason the field is writable, so re-deriving must not eat it."""
    _, _, _, workload = await a_workload(db)
    pinned = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.0-patched.1"

    service = WorkloadService(db)
    await service.update(workload.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(engine_runtime_image=pinned)))
    patched = await service.update(
        workload.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(engine_runtime_version="1.4.2"))
    )

    assert patched.serving.engine_runtime_version == "1.4.2"
    assert patched.serving.engine_runtime_image == pinned


async def test_naming_a_runtime_image_in_the_same_patch_wins(db: AsyncSession) -> None:
    _, _, _, workload = await a_workload(db)
    pinned = "my.registry/vllm:dev"

    patched = await WorkloadService(db).update(
        workload.id,
        WorkloadUpdateRequest(serving=WorkloadServingUpdate(engine_runtime_version="1.4.2", engine_runtime_image=pinned)),
    )
    assert patched.serving.engine_runtime_image == pinned


async def test_a_frontend_image_survives_the_api_round_trip(db: AsyncSession) -> None:
    """Declared on create, patched, and cleared — and never confused with the engine image.

    The two images are the same shape and one column apart, which is exactly the
    kind of pair a write path collapses by accident. Clearing matters as much as
    setting: null is how a workload hands the choice back to the cluster once it no
    longer needs a build of its own, and a patch that could not say null would
    leave a dead image address pinned to the model for good.
    """
    cluster, project, artifact = await an_environment(db)
    service = WorkloadService(db)
    pinned = "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2"
    created = await service.create(
        WorkloadCreateRequest(
            cluster=cluster.id,
            serving=a_serving(
                artifact.id, engine_runtime_version="1.4.0-patched.1", frontend_image=pinned
            ),
            name="kimi-k3-mxfp4-tp8-1024k",
            project=project.id,
        )
    )
    assert created.serving.frontend_image == pinned
    assert created.serving.engine_runtime_image == "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.0-patched.1"
    assert (await service.get(created.id)).serving.frontend_image == pinned

    later = "ghcr.io/hangar-dev/vllm-runtime:1.4.1-patched.1-hgr3"
    patched = await service.update(
        created.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(frontend_image=later))
    )
    assert patched.serving.frontend_image == later
    assert patched.serving.engine_runtime_image == created.serving.engine_runtime_image
    assert_patched(created, patched)

    cleared = await service.update(
        created.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(frontend_image=None))
    )
    assert cleared.serving.frontend_image is None
    assert cleared.serving.engine_runtime_image == created.serving.engine_runtime_image


async def test_bumping_the_runtime_version_moves_the_worker_and_leaves_the_frontend(db: AsyncSession) -> None:
    """The two images move on different schedules, and the update path must let them.

    `update` re-derives `engine_runtime_image` from the new version whenever the stored one is
    the plane's own derivation, which is what makes a version bump reach the cluster.
    `frontend_image` has no derivation to re-run and must be left exactly where it is:
    a frontend pinned because it is the only build that tokenizes this model is pinned
    against the engine moving, not with it. Re-deriving it, or clearing it on the way
    past, would put Kimi K3 back on the frontend that answered `/v1/models` empty — and
    the engine-image tests above cannot see that, because they never set the field.
    """
    _, _, _, workload = await a_workload(db)
    pinned = "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2"

    service = WorkloadService(db)
    await service.update(workload.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(frontend_image=pinned)))
    patched = await service.update(
        workload.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(engine_runtime_version="1.4.2"))
    )

    assert patched.serving.engine_runtime_version == "1.4.2"
    assert patched.serving.engine_runtime_image == "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.2"
    assert patched.serving.frontend_image == pinned


async def test_a_workload_without_a_frontend_image_reads_back_null(db: AsyncSession) -> None:
    """The ordinary workload declares nothing, and nothing is what it must report.

    Not the engine image: what a cluster serves its frontends through is the
    operator's fact, and a plane that guessed at it would be right only for the
    clusters where the two happen to be published together.
    """
    _, _, _, workload = await a_workload(db)
    assert workload.serving.engine_runtime_image is not None
    assert workload.serving.frontend_image is None


async def test_the_frontend_image_reaches_the_cluster_on_the_desired_stream(db: AsyncSession) -> None:
    """A frontend image nobody on a cluster can read is a field that does nothing.

    The operator is what renders the Frontend component, so this frame is the only
    place the declaration can arrive. It arrives inlined on the deployment now —
    there is no second frame to keep in step with it.
    """
    cluster, _, _, deployment = await a_deployment(db)
    pinned = "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2"

    frames = [item async for item in stream_desired(db, cluster.id, "demand")]
    payloads = [payload for kind, payload in frames if kind == "deployment.desired"]
    assert [p.serving.frontend_image for p in payloads] == [None]

    await WorkloadService(db).update(
        deployment.workload_id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(frontend_image=pinned))
    )

    frames = [item async for item in stream_desired(db, cluster.id, "demand")]
    payloads = [payload for kind, payload in frames if kind == "deployment.desired"]
    assert [p.serving.frontend_image for p in payloads] == [pinned]
    assert payloads[0].serving.engine_runtime_image == "nvcr.io/nvidia/ai-dynamo/vllm-runtime:0.6.0"


async def test_the_overlay_reaches_the_cluster_on_the_desired_stream(db: AsyncSession) -> None:
    cluster, _, _, deployment = await a_deployment(db)
    await WorkloadService(db).update(
        deployment.workload_id,
        WorkloadUpdateRequest(serving=WorkloadServingUpdate(overlay=OverlaySchema(dynamo={"router": {"mode": "kv"}}))),
    )

    frames = [item async for item in stream_desired(db, cluster.id, "demand")]
    payloads = [payload for kind, payload in frames if kind == "deployment.desired"]

    assert [p.serving.overlay.dynamo for p in payloads] == [{"router": {"mode": "kv"}}]
    assert payloads[0].serving.overlay.pod_spec == {}


async def test_pools_lifecycle(db: AsyncSession) -> None:
    cluster = await a_cluster(db)
    service = PoolService(db)
    created = await service.create(PoolCreateRequest(cluster=cluster.id, name="h100-pool", device_class="h100"))
    assert created.membership.nodes_total == 0

    assert (await service.get(created.id)).name == "h100-pool"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, PoolUpdateRequest(min_nodes=1, max_nodes=4))
    assert (patched.min_nodes, patched.max_nodes) == (1, 4)
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_quotas_lifecycle(db: AsyncSession) -> None:
    project = await a_project(db)
    service = QuotaService(db)
    created = await service.create(QuotaCreateRequest(project=project.id, device_class="h100", guaranteed_devices=4))
    assert created.usage.allocated_devices == 0

    assert (await service.get(created.id)).device_class == "h100"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, QuotaUpdateRequest(guaranteed_devices=8, limit_devices=16))
    assert (patched.guaranteed_devices, patched.limit_devices) == (8, 16)
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_policies_lifecycle(db: AsyncSession) -> None:
    cluster = await a_cluster(db)
    service = PolicyService(db)
    created = await service.create(
        PolicyCreateRequest(
            name="eu-only",
            allowed_regions=["eu-west-1"],
            attachments=[PolicyAttachmentRequest(subject_type="cluster", subject=cluster.id)],
        )
    )
    assert [a.subject for a in created.attachments] == [cluster.id]

    assert (await service.get(created.id)).name == "eu-only"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, PolicyUpdateRequest(enabled=False, attachments=[]))
    assert patched.enabled is False
    assert patched.attachments == []
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_components_lifecycle(db: AsyncSession) -> None:
    cluster = await a_cluster(db)
    service = ComponentService(db)
    created = await service.create(
        ComponentCreateRequest(cluster=cluster.id, name="agent", kind="agent", desired_version="1.0.0")
    )
    assert created.cluster_id == cluster.id

    assert (await service.get(created.id)).desired_version == "1.0.0"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, ComponentUpdateRequest(desired_version="1.1.0"))
    assert patched.desired_version == "1.1.0"
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_a_deployment_write_announces_itself_to_the_hub(db: AsyncSession) -> None:
    """The wakeup that turns a write into something a cluster hears about now.

    Asserted through an injected notifier rather than a real `LISTEN`, because the
    thing worth pinning down is that every write path announces, and announces the
    cluster the object actually belongs to. Whether the notification arrives is
    explicitly not a guarantee — resync covers a lost one.
    """
    notifier = MemoryNotifier()
    cluster, _, _, created = await a_deployment(db)
    service = DeploymentService(db, notifier=notifier)

    await service.update(created.id, DeploymentUpdateRequest(enabled=False))
    await WorkloadService(db, notifier=notifier).delete(created.workload_id)

    assert [(c.cluster_id, c.kind, c.uid) for c in notifier.changes] == [
        (cluster.id, "deployment", created.id),
        (cluster.id, "deployment", created.id),
    ]


async def test_resuming_a_workload_announces_the_clusters_it_was_parked_on(db: AsyncSession) -> None:
    """The gap this closes: a resume that no cluster ever heard about.

    Pausing announces every deployment it disabled. Resuming used to hand the job to
    `_reconcile`, which announces a cluster only when its share moved — and a pause
    leaves the shares exactly where they were, so a workload coming back to the split
    it left announced nothing. The row read `enabled` while the cluster still had no
    copy of it, and the model never came back up.
    """
    notifier = MemoryNotifier()
    cluster, _, _, created = await a_deployment(db)
    service = WorkloadService(db, notifier=notifier)

    await service.set_paused(created.workload_id, True)
    notifier.changes.clear()
    await service.set_paused(created.workload_id, False)

    assert [(c.cluster_id, c.kind, c.uid) for c in notifier.changes] == [
        (cluster.id, "deployment", created.id)
    ]
    assert (await DeploymentService(db).get(created.id)).enabled is True


async def test_live_dispatch_renders_what_a_resync_would_have_sent(db: AsyncSession) -> None:
    """The divergence this seam exists not to introduce.

    Two ways of reaching a cluster with the same deployment — the snapshot on
    connect and the single frame on a write — have to agree field for field, or a
    reconnect silently changes what is running.
    """
    cluster, _, _, created = await a_deployment(db)

    frames = [item async for item in stream_desired(db, cluster.id, "demand")]
    from_resync = [payload for kind, payload in frames if kind == "deployment.desired"]
    kind, from_dispatch = await desired_object(db, cluster.id, "deployment", created.id)

    assert kind == "deployment.desired"
    assert from_dispatch == from_resync[0]


async def test_live_dispatch_sends_the_dependencies_a_deployment_cannot_reconcile_without(
    db: AsyncSession,
) -> None:
    """The chain is one hop shorter than it was, and the remaining hops still matter.

    A recipe used to be the sharper version of this failure: it announced to every
    cluster with a deployment naming it, which at the moment it was created was
    none, and nothing re-announced it when one finally did — so the cluster held a
    deployment pointing at a recipe it had never been sent and waited for it for
    ever, `RecipeNotDeclared` every fifteen seconds. The serving configuration is
    inlined on the deployment now, so there is nothing left to arrive out of order.

    The artifact and the cache entry are not hypothetical either: the operator will
    not start a replica against a cold cache, so a deployment whose weights nothing
    has declared warm never gets past residency.
    """
    cluster, _, _, created = await a_deployment(db)

    frames = await desired_frames(db, cluster.id, "deployment", created.id)
    kinds = [kind for kind, _ in frames]

    assert kinds == ["artifact.desired", "cache.desired", "deployment.desired"]
    assert kinds.index("artifact.desired") < kinds.index("deployment.desired")
    assert "recipe.desired" not in kinds


async def test_a_deployment_makes_its_own_weights_warm(db: AsyncSession) -> None:
    """Deploying a model is a statement that its weights must be resident.

    The warm set has no writer and never had one, so residency declared only from
    that table meant every deployment of an uncached artifact waited on a download
    nothing had started. A derived entry is unpinned and at priority zero: warm
    because something deploys it, and evictable once nothing does.
    """
    cluster, _, artifact, _ = await a_deployment(db)

    frames = [item async for item in stream_desired(db, cluster.id, "demand")]
    caches = [payload for kind, payload in frames if kind == "cache.desired"]

    assert caches, "a cluster running a deployment declares a cache"
    served = {entry.artifact_id for entry in caches[0].entries}
    assert artifact.id in served
    entry = next(e for e in caches[0].entries if e.artifact_id == artifact.id)
    assert (entry.pin, entry.priority) == (False, 0)


async def test_an_observed_deployment_does_not_make_its_weights_warm(db: AsyncSession) -> None:
    """The plane does not declare cache policy for a workload it does not run.

    An adopted deployment is `observed` by definition: something is already
    serving those weights and they are already on the node. Deriving a warm entry
    from it made the cluster download a second copy of what was running on it, and
    the first version of this union did exactly that to three production models.
    """
    cluster, _, artifact, created = await a_deployment(db)
    service = DeploymentService(db, notifier=MemoryNotifier())
    await service.set_authority(created.id, DeploymentAuthorityRequest(authority="observed"))

    frames = [item async for item in stream_desired(db, cluster.id, "demand")]
    caches = [payload for kind, payload in frames if kind == "cache.desired"]

    served = {entry.artifact_id for cache in caches for entry in cache.entries}
    assert artifact.id not in served


async def test_a_deployment_that_is_gone_dispatches_as_a_deletion(db: AsyncSession) -> None:
    """Absence is the answer, and it has to be the *deleted* answer.

    The row is gone by the time the hub re-reads, so there is nothing to render;
    rendering nothing would leave the cluster serving a deployment nobody declares
    any more.
    """
    cluster, _, _, created = await a_deployment(db)
    await WorkloadService(db, notifier=MemoryNotifier()).delete(created.workload_id)

    kind, payload = await desired_object(db, cluster.id, "deployment", created.id, generation=7)

    assert kind == "deployment.deleted"
    assert (payload.deployment_id, payload.cluster_id, payload.generation) == (created.id, cluster.id, 7)


async def test_deployments_lifecycle(db: AsyncSession) -> None:
    """Read, flip, and disappear with the workload — a deployment has no life of its own."""
    cluster, _, _, created = await a_deployment(db)
    service = DeploymentService(db)
    assert created.cluster_id == cluster.id
    assert created.workload_name == "llama-prod"

    assert (await service.get(created.id)).name == "llama-prod"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, DeploymentUpdateRequest(enabled=False))
    assert patched.enabled is False
    assert_patched(created, patched)

    await WorkloadService(db).delete(created.workload_id)
    assert (await service.list()).count == 0


async def test_workloads_lifecycle(db: AsyncSession) -> None:
    """The object a user actually declares, and the expansion it owns."""
    cluster, project, artifact, created = await a_workload(db)
    service = WorkloadService(db)
    assert created.project_id == project.id
    assert created.serving.artifact_id == artifact.id
    assert [d.cluster_id for d in created.deployments] == [cluster.id]
    assert created.scale.unit == "replicas"
    assert created.scale.placed == 1

    assert (await service.get(created.id)).name == "llama-prod"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, WorkloadUpdateRequest(labels={"tier": "gold"}))
    assert patched.labels == {"tier": "gold"}
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0
    assert (await DeploymentService(db).list()).count == 0


async def test_resolution_follows_targets_to_serving_addresses(db: AsyncSession) -> None:
    cluster, project, _, deployment = await a_deployment(db)
    clusters = ClusterService(db)
    endpoints = EndpointService(db)
    created = await endpoints.create(
        EndpointCreateRequest(
            name="chat",
            project=project.id,
            targets=[EndpointTargetRequest(deployment=deployment.id)],
        )
    )

    # No ClusterServing row at all, which is the state of every cluster we run: the
    # upstream resolves with an empty url and the cluster id is the whole route,
    # because the agent dialled a tunnel out and there is no address to publish.
    bare = await endpoints.resolved()
    assert [e.id for e in bare.endpoints] == [created.id]
    assert [(u.cluster_id, u.url) for u in bare.endpoints[0].upstreams] == [(cluster.id, "")]

    await clusters.set_serving(cluster.id, ClusterServingRequest(url="http://frontend.svc:8000"))
    table = await endpoints.resolved()
    assert table.revision != bare.revision
    upstream = table.endpoints[0].upstreams[0]
    assert upstream.url == "http://frontend.svc:8000"
    assert upstream.cluster_id == cluster.id
    assert upstream.deployment_id == deployment.id
    assert upstream.weight == 100

    assert (await endpoints.resolved(created.id)).endpoints[0].id == created.id
    with pytest.raises(NotFound):
        await endpoints.resolved("end_missing")

    await clusters.set_serving(cluster.id, ClusterServingRequest(url="http://frontend.svc:8000", enabled=False))
    disabled_address = await endpoints.resolved()
    assert disabled_address.endpoints[0].upstreams == []

    await clusters.set_serving(cluster.id, ClusterServingRequest(url="http://frontend.svc:8000"))
    await endpoints.update(created.id, EndpointUpdateRequest(enabled=False))
    assert (await endpoints.resolved()).endpoints == []


async def _two_serving_deployments(db: AsyncSession):
    """Two per-cluster deployments of one project, both dialable, for one endpoint to front.

    A single-target endpoint never exercises the part of resolution that matters —
    the fan-out across clusters, the per-target weights and roles, and what
    survives one cluster going quiet — so the routing scenarios below all start
    from a workload that talks to two deployments in two clusters, both serving.
    """
    cluster_a, project, artifact, workload_a = await a_workload(db, name="chat-alpha")
    deployment_a = await DeploymentService(db).get(workload_a.deployments[0].id)
    cluster_b = await ClusterService(db).create(ClusterCreateRequest(name="beta", region="eu-west-1", phase="active"))
    workload_b = await WorkloadService(db, notifier=MemoryNotifier()).create(
        WorkloadCreateRequest(
            cluster=cluster_b.id, serving=a_serving(artifact.id), name="chat-beta", project=project.id
        )
    )
    deployment_b = await DeploymentService(db).get(workload_b.deployments[0].id)
    await ClusterService(db).set_serving(cluster_a.id, ClusterServingRequest(url="http://alpha.svc:8000"))
    await ClusterService(db).set_serving(cluster_b.id, ClusterServingRequest(url="http://beta.svc:8000"))
    return project, cluster_a, deployment_a, cluster_b, deployment_b


async def test_an_endpoint_over_two_clusters_resolves_to_both_upstreams(db: AsyncSession) -> None:
    """One public name, two deployments, two dialable addresses — with weights and roles intact."""
    project, cluster_a, deployment_a, cluster_b, deployment_b = await _two_serving_deployments(db)
    endpoints = EndpointService(db)
    await endpoints.create(
        EndpointCreateRequest(
            name="chat",
            project=project.id,
            targets=[
                EndpointTargetRequest(deployment=deployment_a.id, weight=70),
                EndpointTargetRequest(deployment=deployment_b.id, weight=30, role="canary"),
            ],
        )
    )

    table = await endpoints.resolved()
    upstreams = {u.cluster_id: u for u in table.endpoints[0].upstreams}
    assert set(upstreams) == {cluster_a.id, cluster_b.id}
    assert (upstreams[cluster_a.id].weight, upstreams[cluster_a.id].role) == (70, "primary")
    assert (upstreams[cluster_b.id].weight, upstreams[cluster_b.id].role) == (30, "canary")
    assert upstreams[cluster_a.id].url == "http://alpha.svc:8000"
    assert upstreams[cluster_b.id].deployment_id == deployment_b.id


async def test_one_cluster_going_quiet_drops_its_upstream_and_keeps_the_other(db: AsyncSession) -> None:
    """Turning off one cluster's serving address removes its upstream; the endpoint survives.

    Turning it off and *clearing* it are no longer the same thing, and this is where
    the difference shows. `enabled=False` is somebody saying "not this address" and
    the upstream goes; clearing the row leaves the cluster in exactly the state every
    cluster we run is in — no published address, reached over the tunnel its agent
    dialled — so the upstream stays with an empty url. What stops traffic to a cluster
    outright is its `phase`, which the test below covers.
    """
    project, cluster_a, deployment_a, cluster_b, deployment_b = await _two_serving_deployments(db)
    endpoints = EndpointService(db)
    await endpoints.create(
        EndpointCreateRequest(
            name="chat",
            project=project.id,
            targets=[
                EndpointTargetRequest(deployment=deployment_a.id),
                EndpointTargetRequest(deployment=deployment_b.id),
            ],
        )
    )
    await ClusterService(db).set_serving(cluster_b.id, ClusterServingRequest(url="http://beta.svc:8000", enabled=False))

    table = await endpoints.resolved()
    assert len(table.endpoints) == 1
    assert [u.cluster_id for u in table.endpoints[0].upstreams] == [cluster_a.id]

    await ClusterService(db).clear_serving(cluster_b.id)
    tunnelled = await endpoints.resolved()
    assert {u.cluster_id: u.url for u in tunnelled.endpoints[0].upstreams} == {
        cluster_a.id: "http://alpha.svc:8000",
        cluster_b.id: "",
    }


async def test_draining_a_cluster_takes_its_upstream_out_of_the_table(db: AsyncSession) -> None:
    """A phase is declared intent and traffic is the first thing that has to obey it.

    The serving address is untouched throughout: draining is not the same act as
    withdrawing an address, and either one alone has to be enough to stop traffic.
    """
    project, cluster_a, deployment_a, cluster_b, deployment_b = await _two_serving_deployments(db)
    endpoints = EndpointService(db)
    clusters = ClusterService(db)
    await endpoints.create(
        EndpointCreateRequest(
            name="chat",
            project=project.id,
            targets=[
                EndpointTargetRequest(deployment=deployment_a.id),
                EndpointTargetRequest(deployment=deployment_b.id),
            ],
        )
    )
    before = await endpoints.resolved()
    assert {u.cluster_id for u in before.endpoints[0].upstreams} == {cluster_a.id, cluster_b.id}

    await clusters.update(cluster_b.id, ClusterUpdateRequest(phase="draining"))
    drained = await endpoints.resolved()
    assert [u.cluster_id for u in drained.endpoints[0].upstreams] == [cluster_a.id]
    assert drained.revision != before.revision
    assert (await clusters.get(cluster_b.id)).serving.url == "http://beta.svc:8000"

    await clusters.update(cluster_a.id, ClusterUpdateRequest(phase="disabled"))
    dark = await endpoints.resolved()
    assert [e.name for e in dark.endpoints] == ["chat"]
    assert dark.endpoints[0].upstreams == []

    await clusters.update(cluster_a.id, ClusterUpdateRequest(phase="active"))
    assert [u.cluster_id for u in (await endpoints.resolved()).endpoints[0].upstreams] == [cluster_a.id]


async def test_a_deployment_that_is_not_running_still_appears_for_the_gateway_to_judge(db: AsyncSession) -> None:
    """Admission phase is passed through, not filtered on.

    Whether a deployment is running is observed and can lag what the cluster is
    doing; the gateway is closer to the truth. A placed-but-not-yet-running
    target is still a dialable upstream rather than silently dropped.
    """
    project, _, deployment_a, _, _ = await _two_serving_deployments(db)
    endpoints = EndpointService(db)
    await endpoints.create(
        EndpointCreateRequest(
            name="chat",
            project=project.id,
            targets=[EndpointTargetRequest(deployment=deployment_a.id)],
        )
    )

    upstream = (await endpoints.resolved()).endpoints[0].upstreams[0]
    assert upstream.admission_phase == "placed"
    assert upstream.deployment_id == deployment_a.id


async def test_removing_a_target_moves_the_table_revision_so_a_poller_refetches(db: AsyncSession) -> None:
    """A deletion has to move the revision, or a gateway sits behind a 304 holding a dead route.

    Create and update push the newest stamp forward; a delete only removes one,
    so the table revision folds in a count of live rows to stay sensitive to a
    target that went away.
    """
    project, cluster_a, deployment_a, _, deployment_b = await _two_serving_deployments(db)
    endpoints = EndpointService(db)
    created = await endpoints.create(
        EndpointCreateRequest(
            name="chat",
            project=project.id,
            targets=[
                EndpointTargetRequest(deployment=deployment_a.id),
                EndpointTargetRequest(deployment=deployment_b.id),
            ],
        )
    )
    before = (await endpoints.resolved()).revision

    await endpoints.update(
        created.id, EndpointUpdateRequest(targets=[EndpointTargetRequest(deployment=deployment_a.id)])
    )
    after = await endpoints.resolved()

    assert [u.cluster_id for u in after.endpoints[0].upstreams] == [cluster_a.id]
    assert after.revision != before


async def test_failover_order_is_resolved_into_the_table(db: AsyncSession) -> None:
    """The declared fallback order reaches the gateway, named by the same deployments it routes to."""
    project, _, deployment_a, _, deployment_b = await _two_serving_deployments(db)
    endpoints = EndpointService(db)
    await endpoints.create(
        EndpointCreateRequest(
            name="chat",
            project=project.id,
            routing_strategy="failover",
            targets=[
                EndpointTargetRequest(deployment=deployment_a.id),
                EndpointTargetRequest(deployment=deployment_b.id),
            ],
            failover_order=[deployment_b.id, deployment_a.id],
        )
    )

    resolved = (await endpoints.resolved()).endpoints[0]
    assert resolved.routing_strategy == "failover"
    assert resolved.failover_order == [deployment_b.id, deployment_a.id]


async def test_endpoints_lifecycle(db: AsyncSession) -> None:
    _, project, _, deployment = await a_deployment(db)
    service = EndpointService(db)
    created = await service.create(
        EndpointCreateRequest(
            name="chat",
            project=project.id,
            targets=[EndpointTargetRequest(deployment=deployment.id)],
        )
    )
    assert [t.deployment_id for t in created.targets] == [deployment.id]

    assert (await service.get(created.id)).name == "chat"
    assert (await service.list()).count == 1

    patched = await service.update(
        created.id,
        EndpointUpdateRequest(
            enabled=False,
            targets=[EndpointTargetRequest(deployment=deployment.id, weight=50)],
            failover_order=[deployment.id],
        ),
    )
    assert patched.enabled is False
    assert [t.weight for t in patched.targets] == [50]
    assert patched.failover_order == [deployment.id]
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_operations_create_get_list_cancel(db: AsyncSession) -> None:
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id)
    service = OperationService(db, actor="tester")

    created = await service.create(OperationCreateRequest(type="node.drain", target=node))
    assert created.phase == "pending"
    assert created.target.id == node
    assert created.requested_by == "tester"

    assert (await service.get(created.id)).id == created.id
    assert (await service.list()).count == 1

    cancelled = await service.cancel(created.id)
    assert cancelled.phase == "cancelled"
    assert cancelled.cancel_requested_at is not None
    assert cancelled.updated_at is not None

    with pytest.raises(Conflict):
        await service.cancel(created.id)


async def test_an_operation_can_name_its_target_by_kind_and_name(db: AsyncSession) -> None:
    """The id-or-name rule reaches operations, with the kind written beside the name."""
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id, name="gpu-node-04")
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    created = await service.create(OperationCreateRequest(type="node.drain", target="node:gpu-node-04"))

    assert created.target.id == node
    assert created.target.type == "node"
    assert created.cluster_id == cluster.id


async def test_a_qualified_name_is_still_checked_against_the_type(db: AsyncSession) -> None:
    cluster = await a_cluster(db)
    await seed_node(db, cluster.id, name="gpu-node-04")
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    with pytest.raises(Invalid):
        await service.create(OperationCreateRequest(type="device.reset", target="node:gpu-node-04"))


async def test_a_target_name_that_matches_nothing_is_not_found(db: AsyncSession) -> None:
    await a_cluster(db)
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    with pytest.raises(NotFound):
        await service.create(OperationCreateRequest(type="node.drain", target="node:no-such-node"))


async def test_a_target_name_matching_two_clusters_is_refused_not_guessed(db: AsyncSession) -> None:
    """A node name is unique inside a cluster and nowhere above it.

    Draining an arbitrary one of two machines that share a name is worse than
    refusing, so the ambiguity is a 400 and `cluster` is how it is resolved.
    """
    alpha = await a_cluster(db)
    beta = await a_cluster(db, name="beta")
    await seed_node(db, alpha.id, name="gpu-node-04")
    in_beta = await seed_node(db, beta.id, name="gpu-node-04")
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    with pytest.raises(Invalid):
        await service.create(OperationCreateRequest(type="node.drain", target="node:gpu-node-04"))

    narrowed = await service.create(
        OperationCreateRequest(type="node.drain", target="node:gpu-node-04", cluster="beta")
    )
    assert narrowed.target.id == in_beta


async def test_a_bare_name_is_refused_at_creation(db: AsyncSession) -> None:
    cluster = await a_cluster(db)
    await seed_node(db, cluster.id, name="gpu-node-04")
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    with pytest.raises(Invalid):
        await service.create(OperationCreateRequest(type="node.drain", target="gpu-node-04"))


async def test_an_idempotency_key_replays_through_a_named_target(db: AsyncSession) -> None:
    """The stored operation holds an id; the retry may spell its target either way."""
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id, name="gpu-node-04")
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    first = await service.create(OperationCreateRequest(type="node.drain", target=node), idempotency_key="retry-1")
    again = await service.create(
        OperationCreateRequest(type="node.drain", target="node:gpu-node-04"), idempotency_key="retry-1"
    )

    assert again.id == first.id


async def test_creating_an_operation_announces_it_to_its_cluster(db: AsyncSession) -> None:
    """The hint that reaches the hub holding the cluster's session.

    An operation is not declared state, so nothing else would ever carry it down:
    it is not in the resync snapshot and no other write path names it.
    """
    notifier = MemoryNotifier()
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id)
    service = OperationService(db, actor="tester", notifier=notifier)

    created = await service.create(OperationCreateRequest(type="node.drain", target=node))

    assert [(c.cluster_id, c.kind, c.uid) for c in notifier.changes] == [(cluster.id, "operation", created.id)]


async def test_creating_an_operation_with_nobody_listening_still_succeeds(db: AsyncSession) -> None:
    """The default notifier talks to Postgres, and no hub is subscribed in a test."""
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id)

    created = await OperationService(db, actor="tester").create(OperationCreateRequest(type="node.cordon", target=node))

    assert created.phase == "pending"


@pytest.mark.parametrize(("type_name", "direction"), [("node.cordon", "true"), ("node.uncordon", "false")])
async def test_cordon_and_uncordon_are_one_wire_kind_told_apart_by_a_parameter(
    db: AsyncSession, type_name: str, direction: str
) -> None:
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id)
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    created = await service.create(OperationCreateRequest(type=type_name, target=node))
    rendered = await operation_desired(db, created.id)

    assert rendered is not None
    kind, payload = rendered
    assert kind == "operation.desired"
    assert payload.kind == "cordon"
    assert payload.parameters["cordon"] == direction
    assert payload.cluster_id == cluster.id
    assert payload.target.kind == "node"
    assert payload.target.id == node
    assert payload.requested_by == "tester"


async def test_operator_supplied_parameters_survive_the_injected_one(db: AsyncSession) -> None:
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id)
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    created = await service.create(
        OperationCreateRequest(type="node.uncordon", target=node, parameters={"grace_period_seconds": "1800"})
    )
    rendered = await operation_desired(db, created.id)

    assert rendered is not None
    assert rendered[1].parameters == {"cordon": "false", "grace_period_seconds": "1800"}


async def test_only_open_operations_are_replayed_to_a_reconnecting_cluster(db: AsyncSession) -> None:
    """Terminal operations are finished business, and re-sending one would redo it."""
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id)
    other = await a_cluster(db, name="beta")
    other_node = await seed_node(db, other.id, name="node-b")
    service = OperationService(db, actor="tester", notifier=MemoryNotifier())

    pending = await service.create(OperationCreateRequest(type="node.drain", target=node))
    running = await service.create(OperationCreateRequest(type="node.cordon", target=node))
    finished = await service.create(OperationCreateRequest(type="node.reboot", target=node))
    elsewhere = await service.create(OperationCreateRequest(type="node.drain", target=other_node))
    await db.execute(
        text("UPDATE ops_operation SET phase = :phase WHERE uid = :uid"), {"phase": "running", "uid": running.id}
    )
    await db.execute(
        text("UPDATE ops_operation SET phase = :phase WHERE uid = :uid"), {"phase": "succeeded", "uid": finished.id}
    )
    await db.commit()

    replayed = [payload.operation_id async for _, payload in open_operations(db, cluster.id)]

    assert replayed == [pending.id, running.id]
    assert elsewhere.id not in replayed
    assert [p.operation_id async for _, p in open_operations(db, other.id)] == [elsewhere.id]


@pytest.mark.parametrize("phase", sorted(TERMINAL_PHASES))
async def test_no_terminal_phase_is_ever_replayed(db: AsyncSession, phase: str) -> None:
    cluster = await a_cluster(db)
    node = await seed_node(db, cluster.id)
    created = await OperationService(db, actor="tester", notifier=MemoryNotifier()).create(
        OperationCreateRequest(type="node.drain", target=node)
    )
    await db.execute(
        text("UPDATE ops_operation SET phase = :phase WHERE uid = :uid"), {"phase": phase, "uid": created.id}
    )
    await db.commit()

    assert [item async for item in open_operations(db, cluster.id)] == []


# --------------------------------------------------------------------------- concurrency


async def test_stale_if_match_conflicts(db: AsyncSession) -> None:
    """A second patch holding the first patch's revision is refused.

    This only means anything against a database: the revision comes from
    `updated_at`, which the database writes and the service has to read back.
    """
    service = ClusterService(db)
    created = await service.create(ClusterCreateRequest(name="alpha", region="eu-west-1"))
    patched = await service.update(created.id, ClusterUpdateRequest(phase="active"), if_match=created.revision)

    with pytest.raises(Conflict):
        await service.update(created.id, ClusterUpdateRequest(phase="draining"), if_match=created.revision)

    fresh = await service.update(created.id, ClusterUpdateRequest(phase="draining"), if_match=patched.revision)
    assert fresh.phase == "draining"
    assert_patched(patched, fresh)


async def test_stale_if_match_conflicts_on_every_module(db: AsyncSession) -> None:
    """The same check for the modules whose `_revision` is its own copy of the helper."""
    cluster, project, artifact = await an_environment(db)
    workload = await WorkloadService(db).create(
        WorkloadCreateRequest(cluster=cluster.id, serving=a_serving(artifact.id), name="llama-prod", project=project.id)
    )

    with pytest.raises(Conflict):
        await ProjectService(db).update(project.id, ProjectUpdateRequest(name="x"), if_match="0")
    with pytest.raises(Conflict):
        await ArtifactService(db).update(artifact.id, ArtifactUpdateRequest(name="x"), if_match="0")
    with pytest.raises(Conflict):
        await WorkloadService(db).update(workload.id, WorkloadUpdateRequest(name="x"), if_match="0")


async def test_repeated_patches_keep_moving_the_stamp(db: AsyncSession) -> None:
    """Two patches in one session: the second must not see the first's expired column."""
    service = PoolService(db)
    cluster = await a_cluster(db)
    pool = await service.create(PoolCreateRequest(cluster=cluster.id, name="h100-pool", device_class="h100"))

    first = await service.update(pool.id, PoolUpdateRequest(min_nodes=1))
    second = await service.update(pool.id, PoolUpdateRequest(min_nodes=2), if_match=first.revision)

    assert second.min_nodes == 2
    assert second.updated_at >= first.updated_at


async def test_a_serving_change_announces_to_the_cluster_running_it(db: AsyncSession) -> None:
    """A serving change is the one write that changes the process a cluster is running.

    It used to be a fan-out nothing else needed: a recipe had no cluster of its own,
    so every cluster with a deployment naming it had to be found and told exactly
    once. The configuration is on the workload now, so the reachable set is the
    workload's own expansion — and the cluster still has to be told, or it keeps
    rendering the old engine configuration for ever.
    """
    notifier = MemoryNotifier()
    cluster, _, _, created = await a_deployment(db)

    await WorkloadService(db, notifier=notifier).update(
        created.workload_id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(engine_runtime_version="0.7.0"))
    )

    assert [(c.cluster_id, c.kind, c.uid) for c in notifier.changes] == [(cluster.id, "deployment", created.id)]
    assert (await DeploymentService(db).get(created.id)).generation == created.generation + 1


async def test_a_workload_no_cluster_took_announces_to_nobody(db: AsyncSession) -> None:
    """Nothing to wake is not an error — there is no cluster that has been told about it."""
    notifier = MemoryNotifier()
    _, project, artifact = await an_environment(db)
    created = await WorkloadService(db).create(
        WorkloadCreateRequest(serving=a_serving(artifact.id), name="unplaced", project=project.id)
    )
    assert created.deployments == []

    await WorkloadService(db, notifier=notifier).update(
        created.id, WorkloadUpdateRequest(serving=WorkloadServingUpdate(engine_runtime_version="0.7.0"))
    )

    assert notifier.changes == []


async def test_a_kind_live_dispatch_does_not_carry_answers_nothing(db: AsyncSession) -> None:
    """One dispatchable object, and inventing a second would say twice what it says.

    There is no `recipe.desired` and no `recipe.deleted` to keep in step any more: a
    serving configuration reaches a cluster inlined on the deployment, and the
    deployment's own frame is the one the cluster acts on.
    """
    cluster, _, artifact, _ = await a_deployment(db)

    assert await desired_object(db, cluster.id, "artifact", artifact.id) is None
    assert await desired_object(db, cluster.id, "recipe", artifact.id) is None


async def test_a_workloads_topology_requirement_round_trips(db: AsyncSession) -> None:
    """Declared once, on the shape, and readable back.

    It used to be declared twice — a requirement on the recipe and a nullable
    override of it on the workload — so a null meant "the recipe decides" rather
    than "no". There is one level left and the column is the answer.
    """
    _, _, artifact, created = await a_workload(
        db,
        name="gang-8",
        serving=None,
    )
    assert created.serving.shape.requires_single_topology_domain is False

    read = await WorkloadService(db).update(
        created.id,
        WorkloadUpdateRequest(
            serving=WorkloadServingUpdate(
                shape=ShapeSchema(
                    device_class="h100",
                    requires_single_topology_domain=True,
                    topology_domain_kind="nvswitch",
                )
            )
        ),
    )
    assert read.serving.shape.requires_single_topology_domain is True
    assert read.serving.shape.topology_domain_kind == "nvswitch"

    reread = await WorkloadService(db).get(created.id)
    assert reread.serving.shape.requires_single_topology_domain is True
    assert reread.serving.shape.topology_domain_kind == "nvswitch"
    assert artifact is not None


async def test_a_workload_without_a_topology_requirement_asks_for_none(db: AsyncSession) -> None:
    """No requirement, not an absent one: the column is `NOT NULL` and defaults to false."""
    _, _, _, created = await a_workload(db, name="plain")
    assert created.serving.shape.requires_single_topology_domain is False
    assert created.serving.shape.topology_domain_kind is None


# ------------------------------------------------------- the deployment timeline


async def _timeline(db: AsyncSession, deployment_uid: str) -> list[tuple[str, str]]:
    """Every event filed against one deployment, oldest first, as `(type, message)`."""
    listed = await EventService(db).list(subject=deployment_uid, limit=100)
    return [(e.kind, e.message) for e in reversed(listed.items)]


async def test_creating_a_deployment_leaves_a_record_of_it_having_been_asked_for(db: AsyncSession) -> None:
    """The first minutes of a deployment used to be unreadable: a row, and nothing about it."""
    cluster, _, _artifact, created = await a_deployment(db)

    timeline = await _timeline(db, created.id)
    assert [kind for kind, _ in timeline] == ["deployment.created"]
    assert cluster.name in timeline[0][1]


async def test_the_creation_event_is_committed_with_the_deployment_and_not_after_it(
    db: AsyncSession,
) -> None:
    """One fact, one transaction.

    Rolling back afterwards is the check: an event written in a transaction of
    its own would not survive it, and the pair it would leave behind — a
    deployment that exists with nothing recording it having been asked for — is
    exactly the gap this event closes.
    """
    _, _, _, created = await a_deployment(db)
    await db.rollback()

    assert [kind for kind, _ in await _timeline(db, created.id)] == ["deployment.created"]


async def test_updating_a_workload_is_recorded_against_every_cluster_it_runs_on(db: AsyncSession) -> None:
    """The timeline is keyed by deployment, so a global change files one entry per cluster."""
    _, _, _, created = await a_workload(db)
    await WorkloadService(db, notifier=MemoryNotifier()).update(
        created.id, WorkloadUpdateRequest(labels={"tier": "gold"})
    )

    timeline = await _timeline(db, created.deployments[0].id)
    assert [kind for kind, _ in timeline] == ["deployment.created", "deployment.updated"]
    assert "llama-prod" in timeline[-1][1]


async def test_a_deleted_deployment_keeps_its_timeline(db: AsyncSession) -> None:
    """`subject_id` carries no foreign key precisely so the record outlives its subject."""
    _, _, _, created = await a_deployment(db)
    await WorkloadService(db, notifier=MemoryNotifier()).delete(created.workload_id)

    kinds = [kind for kind, _ in await _timeline(db, created.id)]
    assert kinds == ["deployment.created", "deployment.deleted"]


async def test_the_serving_block_a_cluster_receives_can_actually_fetch_the_weights(db: AsyncSession) -> None:
    """The frame is well-formed and still useless without this field.

    `engine_runtime_image` is what the operator fetches an artifact with, and it was
    optional the whole way down: no default in the plane, no rejection anywhere,
    and an empty box in the console form sending `null`. The cluster then held a
    warm-set entry it had no downloader for, so the prefetch job was never created
    and the deployment waited on `WeightsNotResident` for ever. Nothing in the
    dispatch path noticed, because every frame it sent was valid.

    The fixture workload declares no image, which is exactly the shape that broke.
    """
    cluster, _, _, created = await a_deployment(db)

    frames = await desired_frames(db, cluster.id, "deployment", created.id)
    deployments = [payload for kind, payload in frames if kind == "deployment.desired"]

    assert deployments, "a deployment's serving configuration reaches the cluster"
    assert deployments[0].serving.engine_runtime_image, "the cluster cannot fetch weights without one"
    assert ":" in deployments[0].serving.engine_runtime_image, "an image without a tag is not an address"


async def test_a_workload_never_reaches_a_cluster_naming_the_latest_tag(db: AsyncSession) -> None:
    """`latest` is not a tag these registries publish; it is an ImagePullBackOff.

    The console offers a version box and `latest` is what gets typed into it. Taken
    verbatim it fails on the cluster minutes later, which is the slowest possible
    place to learn it.
    """
    cluster, project, artifact = await an_environment(db)
    workload = await WorkloadService(db).create(
        WorkloadCreateRequest(
            cluster=cluster.id,
            serving=a_serving(artifact.id, engine_runtime_version="latest"),
            name="latest-vllm",
            project=project.id,
        )
    )

    assert workload.serving.engine_runtime_image is not None
    assert not workload.serving.engine_runtime_image.endswith(":latest")
