"""P8.3: a preset workload is stored as declared, shown expanded, and replays unchanged."""

import pytest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Workload
from app.core.presets import preset_images
from app.core.exceptions import Invalid
from app.modules.manifest.services import ManifestService

from tests.test_modules_db import a_workload
from tests.test_manifest_db import _wire
from tests.test_manifest_db import _container_body


pytestmark = pytest.mark.database


def _preset_body(artifact: str, cluster: str, **runtime) -> dict:
    body = _container_body(artifact, cluster)
    body["spec"]["runtime"] = {"kind": "container", "preset": "vllm", **runtime}
    body["spec"]["serving"]["shape"]["deviceCount"] = 1
    return body


async def test_a_preset_workload_is_stored_as_declared_and_read_expanded(db: AsyncSession) -> None:
    cluster, _, artifact, _ = await a_workload(db)
    svc = ManifestService(db)
    m, created = await svc.put_workload("search", "worker", _preset_body(artifact.handle, cluster.name))
    assert created == "created"
    default, image = next(iter(preset_images()["vllm"].items()))
    assert m.spec.runtime.preset == "vllm"
    assert m.spec.runtime.version == default
    assert m.spec.runtime.image is None
    assert m.status.runtime.image == image
    assert m.status.runtime.command == ["vllm", "serve"]
    assert m.status.runtime.readiness_probe["httpGet"]["path"] == "/v1/models"

    spec = (await db.execute(select(Workload.runtime_spec).where(Workload.uid == m.metadata.uid))).scalar_one()
    assert spec["preset"] == "vllm" and spec["image"] == ""

    replayed, result = await svc.put_workload("search", "worker", _wire(m))
    assert result == "unchanged"
    assert replayed.status.serving_digest == m.status.serving_digest


async def test_a_placeholder_digest_refuses_the_workload(db: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENGINE_PRESET_IMAGES", "vllm@0.99.0=docker.io/vllm/vllm-openai:v0.99.0")
    cluster, _, artifact, _ = await a_workload(db)
    with pytest.raises(Invalid, match="no pinned digest"):
        await ManifestService(db).put_workload("search", "worker", _preset_body(artifact.handle, cluster.name))
