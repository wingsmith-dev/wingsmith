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

import pytest
import psycopg

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.ids import NODE
from app.core.ids import TENANT
from app.core.ids import new_uid
from app.db.notify import MemoryNotifier
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.ingestion.desired import desired_object
from app.ingestion.desired import stream_desired
from app.ingestion.operations import open_operations
from app.ingestion.operations import operation_desired
from app.modules.pools.schemas import PoolCreateRequest
from app.modules.pools.schemas import PoolUpdateRequest
from app.modules.pools.services import PoolService
from app.modules.quotas.schemas import QuotaCreateRequest
from app.modules.quotas.schemas import QuotaUpdateRequest
from app.modules.quotas.services import QuotaService
from app.modules.recipes.schemas import RecipeCreateRequest
from app.modules.recipes.schemas import RecipeUpdateRequest
from app.modules.tenants.schemas import TenantCreateRequest
from app.modules.tenants.schemas import TenantUpdateRequest
from app.modules.clusters.schemas import ClusterCreateRequest
from app.modules.clusters.schemas import ClusterUpdateRequest
from app.modules.policies.schemas import PolicyCreateRequest
from app.modules.policies.schemas import PolicyUpdateRequest
from app.modules.policies.schemas import PolicyAttachmentRequest
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.projects.schemas import ProjectUpdateRequest
from app.modules.recipes.services import RecipeService
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
from app.modules.artifacts.services import ArtifactService
from app.modules.components.schemas import ComponentCreateRequest
from app.modules.components.schemas import ComponentUpdateRequest
from app.modules.endpoints.services import EndpointService
from app.modules.operations.schemas import OperationCreateRequest
from app.modules.components.services import ComponentService
from app.modules.deployments.schemas import DeploymentCreateRequest
from app.modules.deployments.schemas import DeploymentUpdateRequest
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


async def a_cluster(db: AsyncSession, name: str = "alpha"):
    return await ClusterService(db).create(ClusterCreateRequest(name=name, region="eu-west-1"))


async def a_project(db: AsyncSession, slug: str = "search"):
    await seed_tenant(db)
    return await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug=slug, name=slug.title()))


async def an_artifact(db: AsyncSession, digest: str = "sha256:aaaa", name: str = "llama"):
    return await ArtifactService(db).create(ArtifactCreateRequest(digest=digest, name=name))


async def a_recipe(db: AsyncSession, artifact_id: str, name: str = "llama-vllm"):
    return await RecipeService(db).create(
        RecipeCreateRequest(
            artifact=artifact_id,
            name=name,
            engine="vllm",
            engine_version="0.6.0",
            device_class="h100",
            context_length=8192,
        )
    )


async def a_deployment(db: AsyncSession, name: str = "llama-prod", **extra):
    cluster = await a_cluster(db)
    project = await a_project(db)
    artifact = await an_artifact(db)
    recipe = await a_recipe(db, artifact.id)
    await PriorityClassService(db).create(
        PriorityClassCreateRequest(name="standard", value=100, is_default=True),
    )
    deployment = await DeploymentService(db).create(
        DeploymentCreateRequest(cluster=cluster.id, recipe=recipe.id, name=name, project=project.id, **extra)
    )
    return cluster, project, recipe, deployment


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


async def test_recipes_lifecycle(db: AsyncSession) -> None:
    artifact = await an_artifact(db)
    service = RecipeService(db)
    created = await a_recipe(db, artifact.id)
    assert created.artifact_id == artifact.id

    assert (await service.get(created.id)).engine == "vllm"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, RecipeUpdateRequest(engine_version="0.7.0"))
    assert patched.engine_version == "0.7.0"
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


async def test_a_recipe_carries_an_overlay_the_plane_never_reads_inside(db: AsyncSession) -> None:
    """Opaque both ways: what goes in comes back out, and a patch replaces it whole."""
    artifact = await an_artifact(db)
    service = RecipeService(db)
    overlay = {"pod_spec": {"containers": [{"name": "engine", "env": [{"name": "NCCL_DEBUG", "value": "INFO"}]}]}}
    created = await service.create(
        RecipeCreateRequest(
            artifact=artifact.id,
            name="llama-overlaid",
            engine="vllm",
            engine_version="0.6.0",
            device_class="h100",
            context_length=8192,
            overlay=overlay,
        )
    )
    assert created.overlay.pod_spec == overlay["pod_spec"]
    assert created.overlay.dynamo == {}

    patched = await service.update(created.id, RecipeUpdateRequest(overlay={"dynamo": {"replicas": 2}}))
    assert patched.overlay.dynamo == {"replicas": 2}
    assert patched.overlay.pod_spec == {}

    plain = await a_recipe(db, artifact.id, name="llama-plain")
    assert (plain.overlay.pod_spec, plain.overlay.dynamo) == ({}, {})


async def test_the_overlay_reaches_the_cluster_on_the_desired_stream(db: AsyncSession) -> None:
    cluster, _, recipe, _ = await a_deployment(db)
    await RecipeService(db).update(recipe.id, RecipeUpdateRequest(overlay={"dynamo": {"router": {"mode": "kv"}}}))

    frames = [item async for item in stream_desired(db, cluster.id, "demand")]
    payloads = [payload for kind, payload in frames if kind == "recipe.desired"]

    assert [p.overlay.dynamo for p in payloads] == [{"router": {"mode": "kv"}}]
    assert payloads[0].overlay.pod_spec == {}


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

    await service.update(created.id, DeploymentUpdateRequest(desired_replicas=1, max_replicas=3))
    await service.delete(created.id)

    assert [(c.cluster_id, c.kind, c.uid) for c in notifier.changes] == [
        (cluster.id, "deployment", created.id),
        (cluster.id, "deployment", created.id),
    ]


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


async def test_a_deployment_that_is_gone_dispatches_as_a_deletion(db: AsyncSession) -> None:
    """Absence is the answer, and it has to be the *deleted* answer.

    The row is gone by the time the hub re-reads, so there is nothing to render;
    rendering nothing would leave the cluster serving a deployment nobody declares
    any more.
    """
    cluster, _, _, created = await a_deployment(db)
    await DeploymentService(db, notifier=MemoryNotifier()).delete(created.id)

    kind, payload = await desired_object(db, cluster.id, "deployment", created.id, generation=7)

    assert kind == "deployment.deleted"
    assert (payload.deployment_id, payload.cluster_id, payload.generation) == (created.id, cluster.id, 7)


async def test_deployments_lifecycle(db: AsyncSession) -> None:
    cluster, project, recipe, created = await a_deployment(db)
    service = DeploymentService(db)
    assert created.workload.project_id == project.id
    assert created.recipe_id == recipe.id
    assert created.cluster_id == cluster.id

    assert (await service.get(created.id)).name == "llama-prod"
    assert (await service.list()).count == 1

    patched = await service.update(created.id, DeploymentUpdateRequest(desired_replicas=1, max_replicas=3))
    assert patched.max_replicas == 3
    assert_patched(created, patched)

    await service.delete(created.id)
    assert (await service.list()).count == 0


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
    project = await a_project(db)
    artifact = await an_artifact(db)
    recipe = await a_recipe(db, artifact.id)

    with pytest.raises(Conflict):
        await ProjectService(db).update(project.id, ProjectUpdateRequest(name="x"), if_match="0")
    with pytest.raises(Conflict):
        await ArtifactService(db).update(artifact.id, ArtifactUpdateRequest(name="x"), if_match="0")
    with pytest.raises(Conflict):
        await RecipeService(db).update(recipe.id, RecipeUpdateRequest(engine_version="x"), if_match="0")


async def test_repeated_patches_keep_moving_the_stamp(db: AsyncSession) -> None:
    """Two patches in one session: the second must not see the first's expired column."""
    service = PoolService(db)
    cluster = await a_cluster(db)
    pool = await service.create(PoolCreateRequest(cluster=cluster.id, name="h100-pool", device_class="h100"))

    first = await service.update(pool.id, PoolUpdateRequest(min_nodes=1))
    second = await service.update(pool.id, PoolUpdateRequest(min_nodes=2), if_match=first.revision)

    assert second.min_nodes == 2
    assert second.updated_at >= first.updated_at


async def test_a_recipe_write_announces_to_every_cluster_running_it(db: AsyncSession) -> None:
    """A recipe has no cluster of its own, so the fan-out has to be derived.

    Two clusters serving the same recipe both keep rendering the old engine
    configuration until they are told, and each of them needs telling exactly
    once — the reachable set is distinct clusters, not deployments.
    """
    notifier = MemoryNotifier()
    alpha, project, recipe, _ = await a_deployment(db)
    beta = await ClusterService(db).create(ClusterCreateRequest(name="beta", region="eu-west-1"))
    await DeploymentService(db, notifier=MemoryNotifier()).create(
        DeploymentCreateRequest(cluster=beta.id, recipe=recipe.id, name="llama-prod-beta", project=project.id)
    )
    await DeploymentService(db, notifier=MemoryNotifier()).create(
        DeploymentCreateRequest(cluster=beta.id, recipe=recipe.id, name="llama-canary", project=project.id)
    )

    await RecipeService(db, notifier=notifier).update(recipe.id, RecipeUpdateRequest(engine_version="0.7.0"))

    assert sorted((c.cluster_id, c.kind, c.uid) for c in notifier.changes) == sorted(
        [(alpha.id, "recipe", recipe.id), (beta.id, "recipe", recipe.id)]
    )


async def test_a_recipe_no_deployment_names_announces_to_nobody(db: AsyncSession) -> None:
    """Nothing to wake is not an error — there is no cluster that has been told about it."""
    notifier = MemoryNotifier()
    artifact = await an_artifact(db)
    service = RecipeService(db, notifier=notifier)
    created = await a_recipe(db, artifact.id)

    await service.update(created.id, RecipeUpdateRequest(engine_version="0.7.0"))

    assert notifier.changes == []


async def test_live_dispatch_renders_the_recipe_a_resync_would_have_sent(db: AsyncSession) -> None:
    """The same invariant the deployment path rests on, for the object the operator renders from."""
    cluster, _, recipe, _ = await a_deployment(db)
    await RecipeService(db, notifier=MemoryNotifier()).update(recipe.id, RecipeUpdateRequest(engine_version="0.7.0"))

    frames = [item async for item in stream_desired(db, cluster.id, "demand")]
    from_resync = [payload for kind, payload in frames if kind == "recipe.desired"]
    kind, from_dispatch = await desired_object(db, cluster.id, "recipe", recipe.id)

    assert kind == "recipe.desired"
    assert from_dispatch == from_resync[0]


async def test_a_recipe_this_cluster_does_not_run_dispatches_nothing(db: AsyncSession) -> None:
    """No `recipe.deleted` exists, and inventing one would say twice what the deployment already said.

    A recipe leaves a cluster's reachable set when the last deployment naming it
    goes, and that deployment's own deletion is the frame the cluster acts on.
    """
    cluster, _, _, _ = await a_deployment(db)
    artifact = await an_artifact(db, digest="sha256:bbbb", name="mistral")
    unrelated = await a_recipe(db, artifact.id, name="mistral-vllm")

    assert await desired_object(db, cluster.id, "recipe", unrelated.id) is None


async def test_a_deployments_topology_requirement_round_trips(db: AsyncSession) -> None:
    """Declared on the deployment, not inherited from the recipe, and readable back."""
    _, _, _, created = await a_deployment(
        db,
        name="gang-8",
        requires_single_topology_domain=True,
        topology_domain_kind="nvswitch",
    )
    assert created.requires_single_topology_domain is True
    assert created.topology_domain_kind == "nvswitch"

    read = await DeploymentService(db).get(created.id)
    assert read.requires_single_topology_domain is True
    assert read.topology_domain_kind == "nvswitch"


async def test_a_deployment_without_a_topology_requirement_leaves_it_null(db: AsyncSession) -> None:
    _, _, _, created = await a_deployment(db, name="plain")
    assert created.requires_single_topology_domain is None
    assert created.topology_domain_kind is None
