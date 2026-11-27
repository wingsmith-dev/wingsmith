"""P4.2: a media container protocol reaches the routing table as a modality, and a serving quota
carries media unit limits to the gateway."""

import pytest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workload
from app.core.serving import output_modality
from app.core.exceptions import Invalid
from app.modules.quotas.schemas import ServingQuotaCreateRequest
from app.modules.quotas.schemas import ServingQuotaUpdateRequest
from app.modules.quotas.services import ServingQuotaService
from app.modules.endpoints.schemas import WorkloadRefSchema
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.manifest.services import ManifestService
from app.modules.endpoints.services import EndpointService

from tests.test_modules_db import a_workload
from tests.test_modules_db import an_environment
from tests.test_manifest_db import _wire
from tests.test_manifest_db import _container_body


pytestmark = pytest.mark.database


async def test_a_speech_worker_routes_as_speech_and_replays_unchanged(db: AsyncSession) -> None:
    cluster, _, artifact = await an_environment(db)
    svc = ManifestService(db)
    body = _container_body(artifact.handle, cluster.name)
    body["spec"]["runtime"]["protocol"] = "openai-audio-speech"
    m, _ = await svc.put_workload("search", "worker", body)
    assert m.spec.runtime.protocol == "openai-audio-speech"
    assert m.spec.serving.api.output_modality == "speech"

    replayed, result = await svc.put_workload("search", "worker", _wire(m))
    assert result == "unchanged"
    assert replayed.status.serving_digest == m.status.serving_digest

    # The routing table reads the stored modality (`output_modality(workload.serving)`).
    serving = (await db.execute(select(Workload.serving).where(Workload.uid == m.metadata.uid))).scalar_one()
    assert output_modality(serving) == "speech"


async def test_a_declared_modality_that_disagrees_with_the_protocol_is_refused(db: AsyncSession) -> None:
    cluster, _, artifact, _ = await a_workload(db)
    body = _container_body(artifact.handle, cluster.name)
    body["spec"]["runtime"]["protocol"] = "openai-audio-transcriptions"
    body["spec"]["serving"]["api"] = {"outputModality": "image"}
    with pytest.raises(Invalid, match="protocol decides"):
        await ManifestService(db).put_workload("search", "worker", body)


async def test_unit_limits_are_stored_updated_and_published_on_the_routing_table(db: AsyncSession) -> None:
    _, project, _, workload = await a_workload(db)
    e = await EndpointService(db).create(
        EndpointCreateRequest(
            name="img",
            project=project.id,
            targets=[EndpointTargetRequest(workload_ref=WorkloadRefSchema(name=workload.name))],
        )
    )
    quotas = ServingQuotaService(db)
    created = await quotas.create(
        ServingQuotaCreateRequest(endpoint=e.id, unit_limits={"image_units": 600}, enforced=True, limit_scope="divided")
    )
    assert created.unit_limits == {"image_units": 600}
    with pytest.raises(Invalid, match="unknown media unit"):
        await quotas.update(created.id, ServingQuotaUpdateRequest(unit_limits={"pixels": 1}))
    updated = await quotas.update(
        created.id, ServingQuotaUpdateRequest(unit_limits={"image_units": 300, "video_seconds": 60})
    )
    assert updated.unit_limits == {"image_units": 300, "video_seconds": 60}

    (endpoint,) = (await EndpointService(db).resolved(e.id)).endpoints
    assert endpoint.limits.unit_limits == {"image_units": 300, "video_seconds": 60}
    assert endpoint.limits.enforced
    (report,) = (await quotas.resolved(replicas=2)).quotas
    assert report.effective.unit_limits == {"image_units": 150, "video_seconds": 30}
