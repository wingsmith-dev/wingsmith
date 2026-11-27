"""The durable change log (ADR 0038): ordering, rollback, tombstones, resourceVersion, compaction."""

import asyncio
import datetime as dt

import pytest
import msgspec

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import changes
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import ChangeLog
from app.hub.messages import DeploymentObserved
from app.ingestion.sink import roll_up_started
from app.core.exceptions import Gone
from app.core.exceptions import Conflict
from app.modules.artifacts.schemas import ArtifactUpdateRequest
from app.modules.workloads.schemas import WorkloadUpdateRequest
from app.modules.artifacts.services import ArtifactService
from app.modules.workloads.services import WorkloadService

from tests.test_modules_db import a_workload
from tests.test_modules_db import an_artifact
from tests.test_ingestion_db import payload


pytestmark = pytest.mark.database


async def _log(db: AsyncSession) -> list[tuple]:
    rows = await db.execute(select(ChangeLog.seq, ChangeLog.kind, ChangeLog.uid, ChangeLog.op).order_by(ChangeLog.seq))
    return [tuple(r) for r in rows]


@pytest.fixture
async def factory(db: AsyncSession, database: str):
    engine = create_async_engine(database, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _draft(db: AsyncSession, digest: str, name: str) -> Artifact:
    artifact = Artifact(uid=f"art_{name}", digest=digest, name=name, slug=name, handle=name)
    db.add(artifact)
    await db.flush()
    changes.record_change(db, artifact, changes.ADDED)
    return artifact


async def test_create_update_delete_log_in_order_and_stamp_resource_version(db: AsyncSession) -> None:
    created = await an_artifact(db)
    assert int(created.revision) > 0
    updated = await ArtifactService(db).update(created.id, ArtifactUpdateRequest(name="renamed"))
    assert int(updated.revision) > int(created.revision)
    await ArtifactService(db).delete(created.id)
    log = await _log(db)
    assert [(kind, op) for _, kind, _, op in log] == [
        ("Artifact", "ADDED"),
        ("Artifact", "MODIFIED"),
        ("Artifact", "DELETED"),
    ]
    assert [seq for seq, *_ in log][:2] == [int(created.revision), int(updated.revision)]


async def test_a_rolled_back_transaction_leaves_no_row(db: AsyncSession) -> None:
    await _draft(db, "sha256:bbbb", "draft")
    await db.rollback()
    assert await _log(db) == []
    assert changes.pending(db.sync_session) == {}


async def test_seqs_become_visible_in_commit_order(db: AsyncSession, factory) -> None:
    async with factory() as first, factory() as second:
        await _draft(first, "sha256:1111", "first")
        await _draft(second, "sha256:2222", "second")
        await second.commit()
        assert [uid for _, _, uid, _ in await _log(db)] == ["art_second"]
        await first.commit()
    log = await _log(db)
    assert [uid for _, _, uid, _ in log] == ["art_second", "art_first"]


async def test_a_commit_waits_for_the_one_ahead_of_it(db: AsyncSession, factory) -> None:
    """The advisory lock serialises the insert-to-commit window, so seq order is commit order."""
    async with factory() as holder, factory() as writer:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": changes.LOCK_KEY})
        await _draft(writer, "sha256:3333", "waiting")
        pending = asyncio.create_task(writer.commit())
        await asyncio.sleep(0.3)
        assert not pending.done()
        await holder.commit()
        await asyncio.wait_for(pending, 5)
    assert [uid for _, _, uid, _ in await _log(db)] == ["art_waiting"]


async def test_workload_delete_writes_a_tombstone(db: AsyncSession) -> None:
    *_, workload = await a_workload(db)
    await WorkloadService(db).delete(workload.id)
    log = await _log(db)
    assert log[-1][1:] == ("Workload", workload.id, "DELETED")


async def test_status_writes_move_resource_version_and_stale_if_match_conflicts(db: AsyncSession) -> None:
    *_, workload = await a_workload(db)
    row = await db.scalar(select(Workload).where(Workload.uid == workload.id))
    before = str(row.resource_version)
    observed = msgspec.structs.replace(
        payload("deployment.observed"), phase="running", observed_at="2000-01-01T00:00:00Z"
    )
    assert isinstance(observed, DeploymentObserved)
    await roll_up_started(db, row.id, observed)
    await db.commit()
    after = (await WorkloadService(db).get(workload.id)).revision
    assert int(after) > int(before)
    with pytest.raises(Conflict):
        await WorkloadService(db).update(workload.id, WorkloadUpdateRequest(labels={"a": "b"}), if_match=before)
    await db.rollback()
    await WorkloadService(db).update(workload.id, WorkloadUpdateRequest(labels={"a": "b"}), if_match=after)


async def test_compaction_makes_old_resume_points_gone(db: AsyncSession) -> None:
    first = await an_artifact(db)
    await an_artifact(db, digest="sha256:cccc", name="second")
    rows, through = await changes.since(db, 0, 10)
    assert len(rows) == 2 and through == 0
    await db.execute(
        text("UPDATE change_log SET created_at = now() - interval '2 days' WHERE seq = :s"), {"s": int(first.revision)}
    )
    await db.commit()
    through = await changes.compact(db, dt.timedelta(days=1))
    assert through == int(first.revision)
    with pytest.raises(Gone):
        await changes.since(db, through, 10)
    rows, _ = await changes.since(db, through + 1, 10)
    assert rows == []
    assert await changes.current(db) > through
