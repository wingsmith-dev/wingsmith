"""Manifests read through the services against a real Postgres: names, not ids, and namespaces held."""

import json

import pytest
import msgspec

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import changes
from app.db.models import Replica
from app.db.models import Workload
from app.db.models import ChangeLog
from app.db.models import Operation
from app.db.models import Deployment
from app.db.notify import DatabaseNotifier
from app.ingestion.sink import serving_moved
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.modules.manifest.schemas import API_VERSION
from app.modules.manifest.schemas import NOTE_ANNOTATION
from app.modules.manifest.schemas import Scale
from app.modules.manifest.schemas import ObjectMeta
from app.modules.manifest.schemas import ScaleReplicas
from app.modules.manifest.schemas import WorkloadManifest
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.artifacts.schemas import ArtifactCreateRequest
from app.modules.artifacts.schemas import ArtifactLocationRequest
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.manifest.services import LAST_APPLIED
from app.modules.manifest.services import ManifestService
from app.modules.manifest.services import merge_removed
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
    assert [(c.type, c.status) for c in m.status.conditions] == [("Ready", "False"), ("Unschedulable", "False")]


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
    _, condition = m.status.conditions
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
    assert created == "created" and m.metadata.generation == 1
    assert m.spec.cluster == cluster.name

    body = _workload_body(artifact.handle, cluster.name, preemptible=True)
    m2, created = await svc.put_workload("search", "applied", body)
    assert created != "created" and m2.spec.preemptible
    assert m2.metadata.generation == 2


async def test_put_workload_replayed_bumps_nothing_and_rolls_nothing(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, labels={"tier": "gold"}, note="first cut")
    svc = ManifestService(db)
    before = await svc.workload("search", "gpt-oss-prod")
    rolled = await _deployment_generations(db, workload.id)
    after, created = await svc.put_workload("search", "gpt-oss-prod", _wire(before))
    assert created != "created"
    assert after.metadata.generation == before.metadata.generation
    # The first apply records last-applied, which is a change; a second identical one is not.
    again, _ = await svc.put_workload("search", "gpt-oss-prod", _wire(before))
    assert again.metadata.resource_version == after.metadata.resource_version
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
    with pytest.raises(Invalid):
        await svc.put_endpoint("search", "chat", {"metadata": {"name": "chat", "namespace": "chat"}, "spec": {}})


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


async def test_put_workload_on_an_observed_workload_is_a_422(db: AsyncSession) -> None:
    _, _, _, deployment = await a_deployment(db)
    await db.execute(Deployment.__table__.update().where(Deployment.uid == deployment.id).values(authority="observed"))
    await db.commit()
    db.expire_all()
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    with pytest.raises(Invalid):
        await svc.put_workload("search", "gpt-oss-prod", body)


async def test_put_workload_accepts_unschedulable_replicas_as_a_condition(db: AsyncSession) -> None:
    await a_workload(db, max_scale=20)
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    body["spec"]["replicas"] = 20
    m, _ = await svc.put_workload("search", "gpt-oss-prod", body)
    assert m.spec.replicas == 20
    _, condition = m.status.conditions
    assert (condition.type, condition.status) == ("Unschedulable", "True")


def _endpoint_body(name: str = "chat", **spec) -> dict:
    return {
        "apiVersion": API_VERSION,
        "kind": "Endpoint",
        "metadata": {"name": name, "namespace": "search"},
        "spec": spec,
    }


async def test_put_endpoint_creates_replaces_and_replays(db: AsyncSession) -> None:
    _, _, _, deployment = await a_deployment(db)
    svc = ManifestService(db)
    body = _endpoint_body(slug="chat-model", targets=[{"deployment": deployment.object_name}])
    m, created = await svc.put_endpoint("search", "chat", body)
    assert created == "created" and [t.deployment for t in m.spec.targets] == [deployment.object_name]

    again, created = await svc.put_endpoint("search", "chat", _wire(m))
    assert created != "created" and again.metadata.generation == m.metadata.generation

    body["spec"]["aliases"] = ["chat-alias"]
    changed, _ = await svc.put_endpoint("search", "chat", body)
    assert changed.spec.aliases == ["chat-alias"]
    assert changed.metadata.generation == m.metadata.generation + 1


async def test_put_endpoint_with_a_workload_ref_to_a_missing_workload_reads_as_a_condition(db: AsyncSession) -> None:
    await a_project(db, "search")
    m, created = await ManifestService(db).put_endpoint(
        "search", "chat", _endpoint_body(slug="later", targets=[{"workloadRef": {"name": "not-yet"}}])
    )
    assert created == "created"
    assert [(c.type, c.status) for c in m.status.conditions] == [("ResolvedRefs", "False"), ("Ready", "False")]


async def test_put_endpoint_with_a_slug_another_endpoint_holds_is_a_conflict(db: AsyncSession) -> None:
    await a_project(db, "search")
    svc = ManifestService(db)
    await svc.put_endpoint("search", "one", _endpoint_body("one", slug="taken"))
    with pytest.raises(Conflict):
        await svc.put_endpoint("search", "two", _endpoint_body("two", slug="taken"))


async def test_put_workload_replicas_only_with_a_stale_revision_is_a_conflict(db: AsyncSession) -> None:
    await a_workload(db, max_scale=5, desired_scale=1)
    svc = ManifestService(db)
    stale = await svc.workload("search", "gpt-oss-prod")
    await svc.put_scale("search", "gpt-oss-prod", _scale(2))
    body = _wire(stale)
    body["spec"]["replicas"] = 3
    with pytest.raises(Conflict):
        await svc.put_workload("search", "gpt-oss-prod", body, if_match=stale.metadata.resource_version)
    assert (await svc.workload("search", "gpt-oss-prod")).spec.replicas == 2


async def test_put_workload_failing_scale_leaves_the_spec_unchanged(db: AsyncSession, monkeypatch) -> None:
    await a_workload(db, max_scale=5, desired_scale=1)
    svc = ManifestService(db)
    before = await svc.workload("search", "gpt-oss-prod")
    body = _wire(before)
    body["spec"]["preemptible"] = not before.spec.preemptible
    body["spec"]["replicas"] = 3

    async def boom(*_args, **_kwargs):
        raise RuntimeError("scale failed")

    monkeypatch.setattr(WorkloadService, "change_scale", boom)
    with pytest.raises(RuntimeError):
        await svc.put_workload("search", "gpt-oss-prod", body)
    await db.rollback()
    after = await svc.workload("search", "gpt-oss-prod")
    assert after.metadata.generation == before.metadata.generation
    assert after.spec.preemptible == before.spec.preemptible


async def test_put_workload_created_paused_is_never_live(db: AsyncSession) -> None:
    cluster, _, artifact, _ = await a_workload(db)
    m, created = await ManifestService(db).put_workload(
        "search", "applied", _workload_body(artifact.handle, cluster.name, paused=True)
    )
    assert created == "created" and m.spec.paused
    rows = (await db.execute(select(Deployment).join(Workload).where(Workload.name == "applied"))).scalars().all()
    assert rows and not any(d.enabled for d in rows)


async def test_put_workload_annotations_without_the_note_clear_it(db: AsyncSession) -> None:
    await a_workload(db, note="first cut")
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    body["metadata"]["annotations"] = {}
    m, _ = await svc.put_workload("search", "gpt-oss-prod", body)
    assert NOTE_ANNOTATION not in m.metadata.annotations


CONTAINER_IMAGE = "ghcr.io/acme/worker@sha256:" + "a" * 64


def _container_body(artifact: str, cluster: str, image: str = CONTAINER_IMAGE) -> dict:
    body = _workload_body(artifact, cluster)
    body["metadata"]["name"] = "worker"
    body["spec"]["runtime"] = {
        "kind": "container",
        "image": image,
        "args": ["--model", "$(WS_MODEL_PATH)"],
        "ports": [{"name": "http", "containerPort": 8000}],
        "protocol": "openai-chat",
    }
    body["spec"]["serving"].pop("contextLength")
    return body


async def test_put_container_workload_round_trips(db: AsyncSession) -> None:
    cluster, project, artifact, _ = await a_workload(db)
    svc = ManifestService(db)
    m, created = await svc.put_workload("search", "worker", _container_body(artifact.handle, cluster.name))
    assert created == "created"
    assert m.spec.runtime.kind == "container"
    assert m.spec.runtime.image == CONTAINER_IMAGE
    assert m.spec.runtime.engine is None
    row = (await db.execute(select(Workload.runtime_kind, Workload.engine).where(Workload.uid == m.metadata.uid))).one()
    assert tuple(row) == ("container", "container")

    replayed, created = await svc.put_workload("search", "worker", _wire(m))
    assert created == "unchanged"
    assert replayed.metadata.generation == m.metadata.generation
    assert replayed.status.serving_digest == m.status.serving_digest

    bumped, _ = await svc.put_workload(
        "search",
        "worker",
        _container_body(artifact.handle, cluster.name, image="ghcr.io/acme/worker@sha256:" + "b" * 64),
    )
    assert bumped.metadata.generation == m.metadata.generation + 1
    assert bumped.status.serving_digest != m.status.serving_digest
    assert bumped.spec.runtime.image.endswith("b" * 64)


async def test_put_refuses_a_runtime_kind_change_and_a_tagged_image(db: AsyncSession) -> None:
    cluster, project, artifact, _ = await a_workload(db)
    svc = ManifestService(db)
    with pytest.raises(Invalid):
        await svc.put_workload(
            "search", "worker", _container_body(artifact.handle, cluster.name, image="ghcr.io/a/w:1")
        )
    await svc.put_workload("search", "applied", _workload_body(artifact.handle, cluster.name))
    body = _container_body(artifact.handle, cluster.name)
    body["metadata"]["name"] = "applied"
    with pytest.raises(Invalid):
        await svc.put_workload("search", "applied", body)


# --------------------------------------------------------------------------- last-applied (ADR 0032)


async def test_put_records_last_applied_and_a_removed_field_resets_on_reapply(db: AsyncSession) -> None:
    cluster, _, artifact, _ = await a_workload(db)
    svc = ManifestService(db)
    body = _workload_body(artifact.handle, cluster.name, preemptible=True, overrides={"a": 1})
    m, result = await svc.put_workload("search", "applied", body)
    assert result == "created"
    assert json.loads(m.metadata.annotations[LAST_APPLIED]) == body
    assert m.spec.preemptible

    del body["spec"]["preemptible"], body["spec"]["overrides"]
    m2, result = await svc.put_workload("search", "applied", body)
    assert result == "configured"
    assert (m2.spec.preemptible, m2.spec.overrides) == (False, {})
    assert m2.spec.priority_class == m.spec.priority_class, "x-defaulted keeps the stored value"
    assert json.loads(m2.metadata.annotations[LAST_APPLIED]) == body


async def test_put_removing_an_immutable_field_the_last_apply_declared_is_a_422(db: AsyncSession) -> None:
    cluster, _, artifact, _ = await a_workload(db)
    svc = ManifestService(db)
    body = _workload_body(artifact.handle, cluster.name)
    await svc.put_workload("search", "applied", body)
    del body["spec"]["cluster"]
    with pytest.raises(Invalid) as exc:
        await svc.put_workload("search", "applied", body)
    assert exc.value.detail == {"field": "spec.cluster"}


async def test_put_without_a_record_keeps_declared_only_behaviour(db: AsyncSession) -> None:
    *_, workload = await a_workload(db, preemptible=True)
    body = _wire(await ManifestService(db).workload("search", "gpt-oss-prod"))
    assert LAST_APPLIED not in body["metadata"]["annotations"]
    del body["spec"]["preemptible"]
    m, result = await ManifestService(db).put_workload("search", "gpt-oss-prod", body)
    assert m.spec.preemptible and result == "unchanged"
    assert LAST_APPLIED in m.metadata.annotations, "the first apply starts the record"


async def test_put_replayed_is_unchanged(db: AsyncSession) -> None:
    cluster, _, artifact, _ = await a_workload(db)
    svc = ManifestService(db)
    body = _workload_body(artifact.handle, cluster.name)
    await svc.put_workload("search", "applied", body)
    _, result = await svc.put_workload("search", "applied", body)
    assert result == "unchanged"
    ep = _endpoint_body(slug="replay")
    await svc.put_endpoint("search", "chat", ep)
    _, result = await svc.put_endpoint("search", "chat", ep)
    assert result == "unchanged"


async def test_put_dry_run_persists_nothing_and_announces_nothing(db: AsyncSession, monkeypatch) -> None:
    *_, workload = await a_workload(db, preemptible=False)
    announced = []

    async def record(self, db, change):
        announced.append(change)

    monkeypatch.setattr(DatabaseNotifier, "changed", record)
    svc = ManifestService(db)
    before = await svc.workload("search", "gpt-oss-prod")
    rolled = await _deployment_generations(db, workload.id)
    body = _wire(before)
    body["spec"]["preemptible"] = True
    body["spec"]["serving"]["contextLength"] = before.spec.serving.context_length * 2
    m, result = await svc.put_workload("search", "gpt-oss-prod", body, dry_run=True)
    assert result == "configured" and m.spec.preemptible
    assert m.metadata.generation == before.metadata.generation + 1, "the would-be object"
    after = await svc.workload("search", "gpt-oss-prod")
    assert (after.spec.preemptible, after.metadata.generation) == (False, before.metadata.generation)
    assert LAST_APPLIED not in after.metadata.annotations
    assert await _deployment_generations(db, workload.id) == rolled
    assert announced == []

    _, result = await svc.put_workload("search", "dry", {**body, "metadata": {"name": "dry"}}, dry_run=True)
    assert result == "created"
    with pytest.raises(NotFound):
        await svc.workload("search", "dry")

    await svc.put_workload("search", "gpt-oss-prod", body)
    assert announced, "a real apply announces after commit"
    assert await _deployment_generations(db, workload.id) != rolled


def test_merge_removed_resets_skips_defaulted_and_exempt_paths() -> None:
    last = {"metadata": {"name": "w"}, "spec": {"replicas": 3, "preemptible": True, "priorityClass": "x"}}
    body = {"metadata": {"name": "w"}, "spec": {"runtime": {"engine": "e", "version": "1"}, "serving": SERVING}}
    out = merge_removed(body, last, WorkloadManifest)
    assert out["spec"]["preemptible"] is False
    assert "replicas" not in out["spec"] and "priorityClass" not in out["spec"]
    out = merge_removed(body, last, WorkloadManifest, exempt={("spec", "preemptible")})
    assert "preemptible" not in out["spec"]


def test_replicas_are_exempt_only_when_autoscaled(monkeypatch) -> None:
    import app.modules.manifest.services as services

    assert services.autoscaled(None) is False
    last = {"metadata": {"name": "w"}, "spec": {"replicas": 3}}
    body = {"metadata": {"name": "w"}, "spec": {"runtime": {"engine": "e", "version": "1"}, "serving": SERVING}}
    assert "replicas" not in merge_removed(body, last, WorkloadManifest, exempt={("spec", "replicas")})["spec"]


SERVING = {"artifact": "a", "shape": {"deviceClass": "h100"}}


async def _ready_replicas(db: AsyncSession, deployment_uid: str, ready: int) -> None:
    row = await db.scalar(select(Deployment).where(Deployment.uid == deployment_uid))
    for i in range(ready):
        db.add(
            Replica(
                uid=f"rep_{deployment_uid[-8:]}{i}",
                deployment_id=row.id,
                cluster_id=row.cluster_id,
                name=f"pod-{i}",
                phase="ready",
                ready=True,
            )
        )
    await db.commit()


async def test_workload_ready_follows_its_ready_replicas(db: AsyncSession) -> None:
    *_, deployment = await a_deployment(db)
    m = await ManifestService(db).workload("search", "gpt-oss-prod")
    ready = next(c for c in m.status.conditions if c.type == "Ready")
    assert (ready.status, ready.reason) == ("False", "ReplicasNotReady")
    await _ready_replicas(db, deployment.id, 1)
    db.expunge_all()
    m = await ManifestService(db).workload("search", "gpt-oss-prod")
    ready = next(c for c in m.status.conditions if c.type == "Ready")
    assert (ready.status, ready.reason) == ("True", "ReplicasReady")


async def test_endpoint_ready_is_routable(db: AsyncSession) -> None:
    *_, deployment = await a_deployment(db)
    svc = ManifestService(db)
    m, _ = await svc.put_endpoint(
        "search", "chat", _endpoint_body(slug="chat-r", targets=[{"workloadRef": {"name": "gpt-oss-prod"}}])
    )
    ready = next(c for c in m.status.conditions if c.type == "Ready")
    assert (ready.status, ready.reason) == ("False", "NoReadyTargets")
    await _ready_replicas(db, deployment.id, 2)
    db.expunge_all()
    ready = next(c for c in (await svc.endpoint("search", "chat")).status.conditions if c.type == "Ready")
    assert (ready.status, ready.reason) == ("True", "Routable")


async def test_a_serving_change_is_logged_on_the_workload_and_its_endpoints(db: AsyncSession) -> None:
    *_, deployment = await a_deployment(db)
    svc = ManifestService(db)
    await svc.put_endpoint(
        "search", "chat", _endpoint_body(slug="chat-s", targets=[{"workloadRef": {"name": "gpt-oss-prod"}}])
    )
    head = await changes.current(db)
    row = await db.scalar(select(Deployment).where(Deployment.uid == deployment.id))
    await serving_moved(db, row.id, row.workload_id)
    await changes.commit(db)
    logged = (await db.execute(select(ChangeLog.kind, ChangeLog.op).where(ChangeLog.seq > head))).all()
    assert sorted(logged) == [("Endpoint", "MODIFIED"), ("Workload", "MODIFIED")]


async def test_delete_manifest_checks_the_field_manager(db: AsyncSession) -> None:
    await a_workload(db)
    svc = ManifestService(db)
    body = _wire(await svc.workload("search", "gpt-oss-prod"))
    await svc.put_workload("search", "gpt-oss-prod", body, manager="k8s:alpha/team-a")
    with pytest.raises(Conflict) as refused:
        await svc.delete_manifest("workload", "search", "gpt-oss-prod", "wings-cli")
    assert refused.value.detail == {"manager": "k8s:alpha/team-a"}
    await svc.delete_manifest("workload", "search", "gpt-oss-prod", "wings-cli", force=True)
    with pytest.raises(NotFound):
        await svc.workload("search", "gpt-oss-prod")


async def test_delete_manifest_passes_its_own_manager_and_unmanaged_objects(db: AsyncSession) -> None:
    _, project, _, deployment = await a_deployment(db)
    svc = ManifestService(db)
    await svc.put_endpoint(
        "search",
        "mine",
        _endpoint_body("mine", slug="mine", targets=[{"deployment": deployment.object_name}]),
        manager="ci",
    )
    await svc.delete_manifest("endpoint", "search", "mine", "ci")
    await EndpointService(db).create(
        EndpointCreateRequest(
            name="loose", project=project.id, targets=[EndpointTargetRequest(deployment=deployment.id)]
        )
    )
    await svc.delete_manifest("endpoint", "search", "loose", "wings-cli")
    with pytest.raises(NotFound):
        await svc.endpoint("search", "loose")
