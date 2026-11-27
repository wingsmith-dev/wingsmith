"""Manifests read through the services against a real Postgres: names, not ids, and namespaces held."""

import pytest
import msgspec

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workload
from app.db.models import Operation
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.modules.manifest.schemas import API_VERSION
from app.modules.manifest.schemas import NOTE_ANNOTATION
from app.modules.manifest.schemas import Scale
from app.modules.manifest.schemas import ObjectMeta
from app.modules.manifest.schemas import ScaleReplicas
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.artifacts.schemas import ArtifactCreateRequest
from app.modules.artifacts.schemas import ArtifactLocationRequest
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.manifest.services import ManifestService
from app.modules.projects.services import ProjectService
from app.modules.artifacts.services import ArtifactService
from app.modules.endpoints.services import EndpointService
from app.modules.workloads.services import WorkloadService

from tests.test_modules_db import a_project
from tests.test_modules_db import a_workload
from tests.test_modules_db import a_deployment


pytestmark = pytest.mark.database


async def test_a_workload_reads_back_as_a_manifest_named_by_names(db: AsyncSession) -> None:
    cluster, project, artifact, workload = await a_workload(db, labels={"tier": "gold"}, note="first cut")
    m = await ManifestService(db).workload("search", "gpt-oss-prod")

    assert (m.api_version, m.kind) == (API_VERSION, "Workload")
    assert (m.metadata.name, m.metadata.namespace, m.metadata.uid) == ("gpt-oss-prod", "search", workload.id)
    assert m.metadata.labels == {"tier": "gold"}
    assert m.metadata.annotations == {NOTE_ANNOTATION: "first cut"}
    assert m.metadata.resource_version == workload.revision
    assert m.metadata.generation == workload.generation

    assert m.spec.cluster == cluster.name, "a cluster is named, not given by id"
    assert m.spec.serving.artifact == artifact.handle
    assert (m.spec.runtime.kind, m.spec.runtime.engine, m.spec.runtime.version) == ("dynamo", "vllm", "0.6.0")
    assert m.spec.priority_class == "standard", "the project default reads back as declared"
    assert m.spec.enabled is None, "enabled is create-only and lives on each deployment afterwards"

    assert m.status.phase == workload.phase
    assert m.status.served_model_name == workload.served_model_name
    assert [d.cluster for d in m.status.deployments] == [cluster.name]

    wire = msgspec.json.decode(msgspec.json.encode(m))
    assert set(wire) == {"apiVersion", "kind", "metadata", "spec", "status"}
    assert {"creationTimestamp", "resourceVersion"} <= set(wire["metadata"])


async def test_a_workload_is_only_reached_in_its_own_namespace_and_by_name(db: AsyncSession) -> None:
    *_, workload = await a_workload(db)
    await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug="chat", name="Chat"))
    svc = ManifestService(db)
    with pytest.raises(NotFound):
        await svc.workload("chat", "gpt-oss-prod")
    with pytest.raises(NotFound):
        await svc.workload("search", workload.id)
    with pytest.raises(NotFound):
        await svc.workload("nowhere", "gpt-oss-prod")


async def test_an_endpoint_targets_deployments_by_object_name(db: AsyncSession) -> None:
    _, project, _, deployment = await a_deployment(db)
    await EndpointService(db).create(
        EndpointCreateRequest(
            name="chat", project=project.id, targets=[EndpointTargetRequest(deployment=deployment.id)]
        )
    )
    m = await ManifestService(db).endpoint("search", "chat")
    assert (m.kind, m.metadata.namespace) == ("Endpoint", "search")
    assert [t.deployment for t in m.spec.targets] == [deployment.object_name]
    assert [t.deployment_id for t in m.status.targets] == [deployment.id]
    await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug="other", name="Other"))
    with pytest.raises(NotFound):
        await ManifestService(db).endpoint("other", "chat")


async def test_an_artifact_is_reached_in_its_scope_only(db: AsyncSession) -> None:
    await a_project(db, "search")
    await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug="chat", name="Chat"))
    artifacts = ArtifactService(db)
    await artifacts.create(
        ArtifactCreateRequest(
            digest="sha256:aaaa",
            name="gpt-oss",
            handle="gpt-oss",
            locations=[ArtifactLocationRequest(scheme="s3", uri="s3://w/gpt", region="eu-west-1")],
        )
    )
    await artifacts.create(
        ArtifactCreateRequest(digest="sha256:bbbb", name="lora", handle="lora", project="chat", private=True)
    )
    svc = ManifestService(db)

    public = await svc.artifact(None, "gpt-oss")
    assert (public.metadata.name, public.metadata.namespace) == ("gpt-oss", None)
    assert public.spec.digest == "sha256:aaaa"
    assert public.status.residency == ["eu-west-1"]

    private = await svc.artifact("chat", "lora")
    assert private.metadata.namespace == "chat"

    for project, name in ((None, "lora"), ("search", "lora"), ("chat", "gpt-oss")):
        with pytest.raises(NotFound):
            await svc.artifact(project, name)


# --------------------------------------------------------------------------- /scale (ADR 0025)


def _scale(replicas: int, resource_version: str | None = None) -> Scale:
    return Scale(
        metadata=ObjectMeta(name="gpt-oss-prod", resource_version=resource_version),
        spec=ScaleReplicas(replicas=replicas),
    )


async def _scale_operations(db: AsyncSession, workload_uid: str) -> list[Operation]:
    rows = await db.scalars(
        select(Operation)
        .join(Workload, Workload.id == Operation.target_id)
        .where(Workload.uid == workload_uid, Operation.type == "workload.scale")
    )
    return list(rows)


async def test_scale_reads_spec_replicas_and_what_was_placed(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, max_scale=20)
    s = await ManifestService(db).scale("search", "gpt-oss-prod")
    assert (s.kind, s.spec.replicas, s.status.replicas) == ("Scale", 1, 1)
    m = await ManifestService(db).workload("search", "gpt-oss-prod")
    assert m.spec.replicas == m.spec.scale.desired == 1
    assert [(c.type, c.status) for c in m.status.conditions] == [("Unschedulable", "False")]


async def test_put_scale_bumps_generation_and_files_an_operation(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, max_scale=20)
    svc = ManifestService(db, actor="alice")
    s = await svc.put_scale("search", "gpt-oss-prod", _scale(3), if_match=workload.revision)
    assert (s.spec.replicas, s.status.replicas) == (3, 3)
    assert s.metadata.generation == workload.generation + 1
    ops = await _scale_operations(db, workload.id)
    assert [(o.phase, o.requested_by, o.payload["parameters"]) for o in ops] == [("succeeded", "alice", {"desired": 3})]


async def test_put_scale_accepts_unschedulable_replicas_as_a_condition(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, max_scale=20)
    s = await ManifestService(db).put_scale("search", "gpt-oss-prod", _scale(20))
    assert s.spec.replicas == 20
    assert s.status.replicas < 20
    m = await ManifestService(db).workload("search", "gpt-oss-prod")
    (condition,) = m.status.conditions
    assert (condition.type, condition.status, condition.reason) == ("Unschedulable", "True", "InsufficientCapacity")


async def test_put_scale_with_a_stale_resource_version_is_a_conflict(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, max_scale=20)
    svc = ManifestService(db)
    with pytest.raises(Conflict):
        await svc.put_scale("search", "gpt-oss-prod", _scale(2), if_match="999")
    await db.rollback()
    with pytest.raises(Conflict):
        await svc.put_scale("search", "gpt-oss-prod", _scale(2, resource_version="999"))
    await db.rollback()
    assert (await svc.scale("search", "gpt-oss-prod")).spec.replicas == 1


async def test_put_scale_replayed_is_a_no_op(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, max_scale=20)
    svc = ManifestService(db)
    first = await svc.put_scale("search", "gpt-oss-prod", _scale(2))
    again = await svc.put_scale("search", "gpt-oss-prod", _scale(2))
    assert again.metadata.generation == first.metadata.generation
    assert len(await _scale_operations(db, workload.id)) == 1


async def test_put_scale_outside_the_bounds_widens_them(db: AsyncSession) -> None:
    await a_workload(db)
    s = await ManifestService(db).put_scale("search", "gpt-oss-prod", _scale(3))
    assert s.spec.replicas == 3
    m = await ManifestService(db).workload("search", "gpt-oss-prod")
    assert (m.spec.scale.min, m.spec.scale.max) == (1, 3)


async def test_put_scale_keeps_the_new_count_when_placement_refuses(db: AsyncSession, monkeypatch) -> None:
    *_, workload = await a_workload(db, max_scale=20)

    async def refuse(self, *args, **kwargs):
        raise Invalid("no cluster fits the gang")

    monkeypatch.setattr(WorkloadService, "_expand", refuse)
    s = await ManifestService(db).put_scale("search", "gpt-oss-prod", _scale(4))
    assert (s.spec.replicas, s.status.replicas) == (4, 1)
    (op,) = await _scale_operations(db, workload.id)
    assert op.result == {"summary": "no cluster fits the gang", "unschedulable": True}
