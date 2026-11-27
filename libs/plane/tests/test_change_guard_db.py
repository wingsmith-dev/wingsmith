"""The single write path (ADR 0037): a writer that skips `record_change` fails the suite."""

import pytest

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.artifacts.schemas import ArtifactUpdateRequest
from app.modules.artifacts.services import ArtifactService

from tests.test_modules_db import an_artifact


pytestmark = pytest.mark.database


async def test_the_guard_fails_a_writer_that_skips_record_change(db: AsyncSession, monkeypatch) -> None:
    created = await an_artifact(db)
    monkeypatch.setattr("app.modules.artifacts.services.record_change", lambda *a: None)
    with pytest.raises(AssertionError, match="no change_log record"):
        await ArtifactService(db).update(created.id, ArtifactUpdateRequest(name="sneaky"))
