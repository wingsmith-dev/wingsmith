"""Manifests read through the services against a real Postgres: names, not ids, and namespaces held."""

import pytest
import msgspec

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.modules.manifest.schemas import API_VERSION
from app.modules.manifest.schemas import NOTE_ANNOTATION
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.artifacts.schemas import ArtifactCreateRequest
from app.modules.artifacts.schemas import ArtifactLocationRequest
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.manifest.services import ManifestService
from app.modules.projects.services import ProjectService
from app.modules.artifacts.services import ArtifactService
from app.modules.endpoints.services import EndpointService

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
