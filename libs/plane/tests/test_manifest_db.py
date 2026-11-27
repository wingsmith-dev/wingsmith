"""Manifests read through the services against a real Postgres: names, not ids, and namespaces held."""

import pytest
import msgspec

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workload
from app.db.models import Operation
from app.db.models import Deployment
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
from tests.test_modules_db import an_artifact
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
    assert {"gangSize", "priorityClass", "replicas"} <= set(wire["spec"])
    assert "deviceClass" in wire["spec"]["serving"]["shape"]
    assert {"servedModelName", "observedGeneration"} - set(wire["status"]) == {"observedGeneration"}
    assert wire["status"]["conditions"][0].keys() >= {"type", "status", "reason", "message", "lastTransitionTime"}


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


# --------------------------------------------------------------------------- PUT (ADR 0029)


def _wire(m) -> dict:
    """A manifest as a client holds it after a GET: status and read-only metadata dropped."""
    body = msgspec.json.decode(msgspec.json.encode(m))
    body.pop("status", None)
    for key in ("uid", "generation", "creationTimestamp", "resourceVersion"):
        body["metadata"].pop(key, None)
    return body


def _workload_body(artifact: str, cluster: str | None = None, **spec) -> dict:
    return {
        "apiVersion": API_VERSION,
        "kind": "Workload",
        "metadata": {"name": "applied", "namespace": "search"},
        "spec": {
            **({"cluster": cluster} if cluster else {}),
            "runtime": {"engine": "vllm", "version": "0.6.0"},
            "serving": {"artifact": artifact, "contextLength": 8192, "shape": {"deviceClass": "h100"}},
            **spec,
        },
    }


async def _deployment_generations(db: AsyncSession, workload_uid: str) -> list[int]:
    w = await WorkloadService(db).get(workload_uid)
    return [d.generation for d in w.deployments]


async def test_put_workload_creates_then_replaces(db: AsyncSession) -> None:
    cluster, project, artifact, _ = await a_workload(db)
    svc = ManifestService(db)
    m, created = await svc.put_workload("search", "applied", _workload_body(artifact.handle, cluster.name))
    assert created and m.metadata.generation == 1
    assert m.spec.cluster == cluster.name

    body = _workload_body(artifact.handle, cluster.name, preemptible=True)
    m2, created = await svc.put_workload("search", "applied", body)
    assert not created and m2.spec.preemptible
    assert m2.metadata.generation == 2


async def test_put_workload_replayed_bumps_nothing_and_rolls_nothing(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, labels={"tier": "gold"}, note="first cut")
    svc = ManifestService(db)
    before = await svc.workload("search", "gpt-oss-prod")
    rolled = await _deployment_generations(db, workload.id)
    after, created = await svc.put_workload("search", "gpt-oss-prod", _wire(before))
    assert not created
    assert after.metadata.generation == before.metadata.generation
    assert after.metadata.resource_version == before.metadata.resource_version
    assert await _deployment_generations(db, workload.id) == rolled


async def test_put_workload_omitted_fields_keep_stored_values(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, max_scale=5, desired_scale=2, preemptible=True)
    body = _wire(await ManifestService(db).workload("search", "gpt-oss-prod"))
    for key in ("replicas", "scale", "preemptible", "priorityClass"):
        body["spec"].pop(key)
    m, _ = await ManifestService(db).put_workload("search", "gpt-oss-prod", body)
    assert (m.spec.replicas, m.spec.scale.max, m.spec.preemptible) == (2, 5, True)
    assert m.metadata.generation == workload.generation


async def test_put_workload_with_a_stale_resource_version_is_a_conflict(db: AsyncSession) -> None:
    await a_workload(db)
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    body["spec"]["preemptible"] = True
    with pytest.raises(Conflict):
        await svc.put_workload("search", "gpt-oss-prod", body, if_match="999")
    body["metadata"]["resourceVersion"] = "999"
    with pytest.raises(Conflict):
        await svc.put_workload("search", "gpt-oss-prod", body)
    current = await svc.workload("search", "gpt-oss-prod")
    body["metadata"]["resourceVersion"] = current.metadata.resource_version
    m, _ = await svc.put_workload("search", "gpt-oss-prod", body)
    assert m.spec.preemptible


async def test_put_workload_refuses_an_immutable_change_by_name(db: AsyncSession) -> None:
    await a_workload(db)
    await an_artifact(db, digest="sha256:cccc", name="other")
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    for path, value in ((("gangSize",), 2), (("placement", "allowedRegions"), ["us-east-1"])):
        changed = msgspec.json.decode(msgspec.json.encode(body))
        node = changed["spec"]
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        with pytest.raises(Invalid) as exc:
            await svc.put_workload("search", "gpt-oss-prod", changed)
        assert exc.value.detail["field"] == "spec." + ".".join(path)


async def test_put_refuses_a_namespace_or_name_that_does_not_match_the_path(db: AsyncSession) -> None:
    cluster, _, artifact, _ = await a_workload(db)
    svc = ManifestService(db)
    body = _workload_body(artifact.handle, cluster.name)
    body["metadata"]["namespace"] = "chat"
    with pytest.raises(Invalid):
        await svc.put_workload("search", "applied", body)
    body["metadata"] = {"name": "other", "namespace": "search"}
    with pytest.raises(Invalid):
        await svc.put_workload("search", "applied", body)


async def test_put_workload_refuses_a_missing_or_hidden_artifact(db: AsyncSession) -> None:
    cluster, *_ = await a_workload(db)
    await ProjectService(db).create(ProjectCreateRequest(tenant="acme", slug="chat", name="Chat"))
    await ArtifactService(db).create(
        ArtifactCreateRequest(digest="sha256:bbbb", name="lora", handle="lora", project="chat", private=True)
    )
    svc = ManifestService(db)
    for ref in ("nowhere", "lora"):
        with pytest.raises(Invalid) as exc:
            await svc.put_workload("search", "applied", _workload_body(ref, cluster.name))
        assert exc.value.detail["field"] == "spec.serving.artifact"


async def test_put_workload_on_an_observed_workload_is_a_conflict(db: AsyncSession) -> None:
    _, _, _, deployment = await a_deployment(db)
    await db.execute(Deployment.__table__.update().where(Deployment.uid == deployment.id).values(authority="observed"))
    await db.commit()
    db.expire_all()
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    with pytest.raises(Conflict):
        await svc.put_workload("search", "gpt-oss-prod", body)


async def test_put_workload_accepts_unschedulable_replicas_as_a_condition(db: AsyncSession) -> None:
    await a_workload(db, max_scale=20)
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    body["spec"]["replicas"] = 20
    m, _ = await svc.put_workload("search", "gpt-oss-prod", body)
    assert m.spec.replicas == 20
    (condition,) = m.status.conditions
    assert (condition.type, condition.status) == ("Unschedulable", "True")
