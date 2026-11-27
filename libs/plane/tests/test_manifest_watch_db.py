"""List and watch manifests (ADR 0039): consistent list RVs, ordered events, coalescing, 410s,
bookmarks, selectors and the watch cap."""

import json
import asyncio
import datetime as dt
import contextlib

import pytest

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import changes
from app.core.exceptions import Gone
from app.core.exceptions import TooManyRequests
from app.core.exceptions import InvalidParameter
from app.modules.manifest import watch as watching
from app.modules.artifacts.schemas import ArtifactUpdateRequest
from app.modules.artifacts.services import ArtifactService

from tests.test_modules_db import a_workload
from tests.test_modules_db import an_artifact


pytestmark = pytest.mark.database

NONE = watching.Selector()


@pytest.fixture
async def factory(db: AsyncSession, database: str):
    engine = create_async_engine(database, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class Hub(watching.Fanout):
    """A fanout with no `LISTEN`: watchers fall back to polling."""

    def subscribe(self) -> asyncio.Event:
        if len(self.waiters) >= self.cap:
            raise TooManyRequests("full")
        event = asyncio.Event()
        self.waiters.add(event)
        return event


def a_watch(factory, rv, timeout=0.5, selector=NONE, collection="artifacts", **kw) -> watching.Watch:
    return watching.Watch(factory, collection, None, selector, rv, timeout, poll=0.05, **kw)


async def events_of(w: watching.Watch, hub=None) -> list[dict]:
    await w.check()
    return [json.loads(line) async for line in await watching.stream(w, hub or Hub())]


async def test_a_list_reports_the_rv_its_items_are_current_as_of(db: AsyncSession, factory) -> None:
    await an_artifact(db)
    listed = await watching.list_manifests(factory, "artifacts", None, NONE)
    assert listed["kind"] == "ArtifactList"
    assert [m["metadata"]["name"] for m in listed["items"]] == ["gpt-oss"]
    assert listed["metadata"]["resourceVersion"] == listed["items"][0]["metadata"]["resourceVersion"]


async def test_a_commit_during_a_list_is_either_in_it_or_after_its_rv(db: AsyncSession, factory) -> None:
    """The list and its RV are one snapshot: a write landing mid-list is never lost between them."""
    await an_artifact(db)
    async with watching.snapshot(factory) as snap:
        rv = await changes.current(snap)
        await an_artifact(db, digest="sha256:bbbb", name="late")
        items = await watching._items(snap, "artifacts", None, NONE)
    assert [m["metadata"]["name"] for m in items] == ["gpt-oss"]
    late = await a_watch(factory, rv).batch(rv)
    assert [(e["type"], e["object"]["metadata"]["name"]) for e in late[0]] == [("ADDED", "late")]


async def test_a_watch_delivers_changes_in_order_with_deletes_and_coalescing(db: AsyncSession, factory) -> None:
    rv = await changes.current(db)
    first = await an_artifact(db)
    svc = ArtifactService(db)
    await svc.update(first.id, ArtifactUpdateRequest(name="renamed"))
    await svc.update(first.id, ArtifactUpdateRequest(name="again"))
    second = await an_artifact(db, digest="sha256:bbbb", name="second")
    await svc.delete(second.id)
    events = await events_of(a_watch(factory, rv))
    kinds = [(e["type"], e["object"]["metadata"].get("uid")) for e in events if e["type"] != "BOOKMARK"]
    # The first artifact's three changes coalesce into one ADDED as it is now; the second was
    # deleted before anything read it, so its ADDED coalesces away and only the tombstone remains.
    assert kinds == [("ADDED", first.id), ("DELETED", second.id)]
    current = (await watching.list_manifests(factory, "artifacts", None, NONE))["items"][0]
    assert events[0]["object"] == current


async def test_a_watch_without_rv_starts_with_the_current_objects(db: AsyncSession, factory) -> None:
    await an_artifact(db)
    events = await events_of(a_watch(factory, None, timeout=0.2))
    assert events[0]["type"] == "ADDED" and events[0]["object"]["kind"] == "Artifact"


async def test_a_watch_streams_changes_made_while_it_is_open(db: AsyncSession, factory) -> None:
    rv = await changes.current(db)
    w = a_watch(factory, rv, timeout=2)
    await w.check()
    lines = await watching.stream(w, Hub())
    got = []

    async def read():
        async for line in lines:
            got.append(json.loads(line))
            if got[-1]["type"] == "MODIFIED":
                return

    reader = asyncio.create_task(read())
    created = await an_artifact(db)
    await asyncio.sleep(0.2)
    await ArtifactService(db).update(created.id, ArtifactUpdateRequest(name="renamed"))
    await asyncio.wait_for(reader, 3)
    assert [e["type"] for e in got if e["type"] != "BOOKMARK"] == ["ADDED", "MODIFIED"]
    await lines.aclose()


async def _compact_first(db: AsyncSession) -> int:
    first = await an_artifact(db)
    await an_artifact(db, digest="sha256:cccc", name="second")
    await db.execute(
        text("UPDATE change_log SET created_at = now() - interval '2 days' WHERE seq = :s"), {"s": int(first.revision)}
    )
    await db.commit()
    return await changes.compact(db, dt.timedelta(days=1))


async def test_a_compacted_rv_is_gone_before_the_stream(db: AsyncSession, factory) -> None:
    through = await _compact_first(db)
    with pytest.raises(Gone):
        await a_watch(factory, through).check()


async def test_compaction_mid_stream_ends_it_with_a_410_error(db: AsyncSession, factory) -> None:
    rv = await changes.current(db)
    w = a_watch(factory, rv, timeout=5)
    await w.check()
    lines = await watching.stream(w, Hub())
    await _compact_first(db)
    await db.execute(text("UPDATE change_log_state SET compacted_through = compacted_through + 1000"))
    await db.commit()
    events = [json.loads(line) async for line in lines]
    assert events[-1]["type"] == "ERROR"
    assert events[-1]["object"]["code"] == 410 and events[-1]["object"]["kind"] == "Status"


async def test_bookmarks_carry_the_current_rv(db: AsyncSession, factory) -> None:
    await an_artifact(db)
    head = await changes.current(db)
    events = await events_of(a_watch(factory, head, timeout=0.5, bookmark=0.1))
    bookmarks = [e for e in events if e["type"] == "BOOKMARK"]
    assert bookmarks and bookmarks[-1]["object"]["metadata"]["resourceVersion"] == str(head)


async def test_selectors_filter_lists_and_watches(db: AsyncSession, factory) -> None:
    rv = await changes.current(db)
    await a_workload(db, labels={"tier": "prod"})
    sel = watching.parse_selector
    listed = await watching.list_manifests(factory, "workloads", None, sel("tier=prod", None, None))
    assert len(listed["items"]) == 1
    for selector in (sel("tier=dev", None, None), sel("tier!=prod", None, None), sel(None, "metadata.name=x", None)):
        assert (await watching.list_manifests(factory, "workloads", None, selector))["items"] == []
        events = await events_of(a_watch(factory, rv, timeout=0.2, selector=selector, collection="workloads"))
        assert [e for e in events if e["type"] != "BOOKMARK"] == []
    events = await events_of(
        a_watch(
            factory,
            rv,
            timeout=0.2,
            selector=sel("tier=prod", "metadata.name=gpt-oss-prod", None),
            collection="workloads",
        )
    )
    assert [e["type"] for e in events if e["type"] != "BOOKMARK"] == ["ADDED"]
    with pytest.raises(InvalidParameter):
        sel("tier in (a)", None, None)
    with pytest.raises(InvalidParameter):
        sel(None, "spec.x=1", None)


async def test_the_watch_cap_refuses_one_more(factory) -> None:
    hub = Hub(cap=1)
    first = await watching.stream(a_watch(factory, 0, timeout=5), hub)
    reader = asyncio.create_task(anext(first))
    await asyncio.sleep(0.1)
    assert len(hub.waiters) == 1
    with pytest.raises(TooManyRequests):
        hub.admit()
    late = [json.loads(line) async for line in await watching.stream(a_watch(factory, 0), hub)]
    assert late[0]["object"]["code"] == 429
    reader.cancel()
    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
        await reader
    await first.aclose()
    assert hub.waiters == set()


async def test_a_stream_dropped_before_iterating_holds_no_slot(factory) -> None:
    hub = Hub(cap=1)
    for _ in range(5):
        lines = await watching.stream(a_watch(factory, 0), hub)
        assert hub.waiters == set()
        del lines
    hub.admit()


async def test_slots_are_released_on_timeout_410_and_failure(db: AsyncSession, factory) -> None:
    hub = Hub(cap=1)
    await events_of(a_watch(factory, await changes.current(db), timeout=0.1), hub)
    assert hub.waiters == set()

    through = await _compact_first(db)
    events = [json.loads(line) async for line in await watching.stream(a_watch(factory, through), hub)]
    assert events[-1]["object"]["code"] == 410 and hub.waiters == set()

    broken = a_watch(factory, 0)

    async def boom(*_):
        raise RuntimeError("boom")

    broken.batch = boom
    events = [json.loads(line) async for line in await watching.stream(broken, hub)]
    assert events[-1]["object"]["code"] == 500 and hub.waiters == set()
