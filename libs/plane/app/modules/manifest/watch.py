"""List and watch manifests over HTTP, the Kubernetes way (ADR 0039).

A list reads every object of a kind and the change log's head in one `REPEATABLE READ` snapshot,
so its `resourceVersion` is exactly what the items reflect. A watch resumes from such an RV: it
reads the log after it in short batches, each in its own snapshot, re-reads the row of every
change and streams one JSON line per event (`{"type", "object"}`). A change whose row has moved
on is skipped, since a later change in the log reports it (coalescing); a deleted row is reported
from the log's tombstone. An RV the log has been compacted past is 410 before the stream starts
and an `ERROR` event with a 410 `Status` once it has; the client relists.

Wakeups come from one `LISTEN` per process, shared by every watcher, with a poll as the fallback
for a lost notification. No session is held between batches.
"""

import json
import random
import asyncio
import contextlib

from typing import Any
from logging import getLogger
from dataclasses import field
from dataclasses import dataclass
from collections.abc import Callable
from collections.abc import AsyncIterator

import msgspec
import psycopg

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import changes
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import ChangeLog
from app.db.notify import listen_dsn
from app.core.exceptions import Gone
from app.core.exceptions import NotFound
from app.core.exceptions import TooManyRequests
from app.core.exceptions import InvalidParameter

from .schemas import API_VERSION
from .services import ManifestService


logger = getLogger(__name__)

COLLECTIONS: dict[str, tuple[str, type]] = {
    "workloads": (changes.WORKLOAD, Workload),
    "endpoints": (changes.ENDPOINT, Endpoint),
    "artifacts": (changes.ARTIFACT, Artifact),
}

BATCH = 500
POLL_SECONDS = 5.0
"""How long a watcher waits for a wakeup before reading the log anyway."""
BOOKMARK_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 1800
QUEUE_SIZE = 1000
"""Events buffered for one watcher. A client that leaves it full for `SLOW_SECONDS` is closed."""
SLOW_SECONDS = 10.0
MAX_WATCHES = 256
"""Open watches per process; one more is a 429."""
RETRY_SECONDS = 2.0


# --------------------------------------------------------------------------------- selectors


@dataclass(frozen=True)
class Selector:
    """`labelSelector` equality terms and the `metadata.name` / `metadata.namespace` fields."""

    equal: tuple[tuple[str, str], ...] = ()
    unequal: tuple[tuple[str, str], ...] = ()
    name: str | None = None
    namespace: str | None = None

    def labels_match(self, labels: dict[str, str]) -> bool:
        return all(labels.get(k) == v for k, v in self.equal) and all(labels.get(k) != v for k, v in self.unequal)

    def names_match(self, name: str, namespace: str | None) -> bool:
        return (self.name is None or self.name == name) and (self.namespace is None or self.namespace == namespace)


def _terms(raw: str | None, param: str) -> list[tuple[str, str, bool]]:
    out = []
    for term in (raw or "").split(","):
        term = term.strip()
        if not term:
            continue
        negate = "!=" in term
        key, sep, value = term.partition("!=" if negate else "==" if "==" in term else "=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise InvalidParameter(f"{param}: only key=value and key!=value terms are supported", {"field": param})
        out.append((key, value, negate))
    return out


def parse_selector(label_selector: str | None, field_selector: str | None, namespace: str | None) -> Selector:
    equal = tuple((k, v) for k, v, neg in _terms(label_selector, "labelSelector") if not neg)
    unequal = tuple((k, v) for k, v, neg in _terms(label_selector, "labelSelector") if neg)
    name = None
    for key, value, negate in _terms(field_selector, "fieldSelector"):
        if key == "metadata.name" and not negate:
            name = value
        elif key == "metadata.namespace" and not negate and namespace is None:
            namespace = value
        else:
            raise InvalidParameter(
                "fieldSelector supports metadata.name= and metadata.namespace= only", {"field": "fieldSelector"}
            )
    return Selector(equal, unequal, name, namespace)


# ------------------------------------------------------------------------------------ reads


@contextlib.asynccontextmanager
async def snapshot(sessions: async_sessionmaker) -> AsyncIterator[AsyncSession]:
    """A read-only `REPEATABLE READ` session: every statement sees the same committed state."""
    async with sessions() as db:
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        await db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield db
        finally:
            await db.rollback()


async def _render(db: AsyncSession, kind: str, row, slug: str | None) -> dict | None:
    svc = ManifestService(db)
    try:
        if kind == changes.WORKLOAD:
            m = await svc.workload(slug, row.name)
        elif kind == changes.ENDPOINT:
            m = await svc.endpoint(slug, row.name)
        else:
            m = await svc.artifact(slug, row.name)
    except NotFound:
        return None
    return msgspec.to_builtins(m)


def _project_column(model):
    return model.owner_project_id if model is Artifact else model.project_id


def _columns(model):
    """What a list or watch needs of a row. Columns, not entities: an entity loaded here would sit
    in the identity map and stop the service read from loading its relationships."""
    name = model.handle if model is Artifact else model.name
    return select(model.uid, name.label("name"), _project_column(model).label("project"), model.resource_version)


async def _slugs(db: AsyncSession, ids) -> dict:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return dict((await db.execute(select(Project.id, Project.slug).where(Project.id.in_(ids)))).tuples().all())


async def _project_id(db: AsyncSession, ref: str | None):
    if ref is None:
        return None
    project = await db.scalar(select(Project).where((Project.slug == ref) | (Project.uid == ref)))
    if project is None:
        raise NotFound(f"no project {ref!r}")
    return project.id


async def _items(db: AsyncSession, collection: str, project, selector: Selector) -> list[dict]:
    kind, model = COLLECTIONS[collection]
    query = _columns(model).order_by(model.handle if model is Artifact else model.name, model.id)
    if project is not None:
        query = query.where(_project_column(model) == project)
    if selector.name is not None:
        query = query.where((model.handle if model is Artifact else model.name) == selector.name)
    rows = (await db.execute(query)).all()
    slugs = await _slugs(db, [r.project for r in rows])
    out = []
    for row in rows:
        slug = slugs.get(row.project)
        if selector.namespace is not None and slug != selector.namespace:
            continue
        m = await _render(db, kind, row, slug)
        if m is not None and selector.labels_match(m["metadata"].get("labels") or {}):
            out.append(m)
    return out


async def list_manifests(
    sessions: async_sessionmaker, collection: str, project: str | None, selector: Selector
) -> dict:
    """Every object of a kind, and the RV they are all current as of (`{kind}List`)."""
    async with snapshot(sessions) as db:
        project_id = await _project_id(db, project)
        rv = await changes.current(db)
        items = await _items(db, collection, project_id, selector)
    kind = COLLECTIONS[collection][0]
    return {
        "apiVersion": API_VERSION,
        "kind": f"{kind}List",
        "metadata": {"resourceVersion": str(rv)},
        "items": items,
    }


# ------------------------------------------------------------------------------------ fanout


class Fanout:
    """One `LISTEN` on the change channel per process, waking every watcher.

    Started by the first watcher and stopped with the last. A lost notification costs a poll
    interval, never an event: watchers read the log, not the payload."""

    def __init__(self, dsn: str | None = None, cap: int = MAX_WATCHES, connect: Callable | None = None) -> None:
        self.dsn = dsn
        self.cap = cap
        self.connect = connect
        self.waiters: set[asyncio.Event] = set()
        self.uncounted: set[asyncio.Event] = set()
        self.task: asyncio.Task | None = None

    def admit(self) -> None:
        """429 when every slot is taken; a cheap pre-check before a stream starts."""
        if len(self.waiters) - len(self.uncounted) >= self.cap:
            raise TooManyRequests(f"this plane already serves {self.cap} watches", {"limit": self.cap})

    def subscribe(self, counted: bool = True) -> asyncio.Event:
        """`counted=False` takes no watch slot: the hub's per-cluster manifest feed, bounded by the
        number of connected clusters, not by clients."""
        if counted:
            self.admit()
        event = asyncio.Event()
        self.waiters.add(event)
        if not counted:
            self.uncounted.add(event)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._listen())
        return event

    def unsubscribe(self, event: asyncio.Event) -> None:
        self.waiters.discard(event)
        self.uncounted.discard(event)
        if not self.waiters and self.task is not None:
            self.task.cancel()
            self.task = None

    def wake(self) -> None:
        for event in self.waiters:
            event.set()

    async def _listen(self) -> None:
        connect = self.connect or (lambda dsn: psycopg.AsyncConnection.connect(dsn, autocommit=True))
        while True:
            try:
                connection = await connect(self.dsn or listen_dsn())
                async with connection:
                    await connection.execute(f"LISTEN {changes.CHANNEL}")
                    self.wake()  # anything committed while we were not listening
                    async for _ in connection.notifies():
                        self.wake()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("manifest.watch.listen_lost", exc_info=True)
            await asyncio.sleep(RETRY_SECONDS)


_fanout: Fanout | None = None


def fanout() -> Fanout:
    global _fanout
    if _fanout is None:
        _fanout = Fanout()
    return _fanout


# ------------------------------------------------------------------------------------- watch


@dataclass
class Watch:
    sessions: async_sessionmaker
    collection: str
    project: str | None
    selector: Selector
    resource_version: int | None
    """Resume after this RV; `None` starts with an `ADDED` per current object."""
    timeout: float
    bookmark: float = BOOKMARK_SECONDS
    poll: float = POLL_SECONDS
    project_id: Any = None
    added: set = field(default_factory=set)
    """uids whose `ADDED` was coalesced into a later change, still owed an `ADDED`."""

    async def check(self) -> None:
        """Raises `Gone` (a plain 410) when the resume point is compacted, before streaming."""
        async with snapshot(self.sessions) as db:
            self.project_id = await _project_id(db, self.project)
            if self.resource_version is not None:
                through = await changes.compacted_through(db)
                if through and self.resource_version <= through:
                    raise Gone(
                        f"resourceVersion {self.resource_version} is too old; the change log is compacted "
                        f"through {through}",
                        {"compacted_through": through},
                    )

    async def batch(self, cursor: int) -> tuple[list[dict], int]:
        """Events after `cursor` up to the log's head (or `BATCH` changes), and the new cursor."""
        kind, model = COLLECTIONS[self.collection]
        async with snapshot(self.sessions) as db:
            through = await changes.compacted_through(db)
            if through and cursor <= through:
                raise Gone(f"resourceVersion {cursor} is too old; the change log is compacted through {through}")
            head = await changes.current(db)
            query = (
                select(ChangeLog)
                .where(ChangeLog.seq > cursor, ChangeLog.seq <= head, ChangeLog.kind == kind)
                .order_by(ChangeLog.seq)
                .limit(BATCH)
            )
            if self.project_id is not None:
                query = query.where(ChangeLog.project_id == self.project_id)
            log = list(await db.scalars(query))
            uids = {c.uid for c in log}
            rows = {r.uid: r for r in (await db.execute(_columns(model).where(model.uid.in_(uids)))).all()}
            slugs = await _slugs(db, [c.project_id for c in log])
            events = []
            for change in log:
                slug = slugs.get(change.project_id)
                row = rows.get(change.uid)
                event = await self._event(db, kind, change, row, slug)
                if event is not None:
                    events.append(event)
        return events, (log[-1].seq if len(log) == BATCH else head)

    async def _event(self, db: AsyncSession, kind: str, change: ChangeLog, row, slug: str | None) -> dict | None:
        name = change.name if row is None else row.name
        if not self.selector.names_match(name, slug):
            return None
        if change.op == changes.DELETED:
            self.added.discard(change.uid)
            # The log keeps no labels, so a delete is sent whatever the labelSelector says.
            metadata = {"name": name, "uid": change.uid, "resourceVersion": str(change.seq)}
            if slug is not None:
                metadata["namespace"] = slug
            if change.generation is not None:
                metadata["generation"] = change.generation
            return {"type": changes.DELETED, "object": {"apiVersion": API_VERSION, "kind": kind, "metadata": metadata}}
        if row is None or row.resource_version != change.seq:
            # Coalesced: a later change in the log reports this object as it is now.
            if change.op == changes.ADDED:
                self.added.add(change.uid)
            return None
        m = await _render(db, kind, row, slug)
        if m is None or not self.selector.labels_match(m["metadata"].get("labels") or {}):
            return None
        op = change.op
        if change.uid in self.added:
            self.added.discard(change.uid)
            op = changes.ADDED
        return {"type": op, "object": m}

    async def initial(self) -> tuple[list[dict], int]:
        listed = await list_manifests(self.sessions, self.collection, self.project, self.selector)
        events = [{"type": changes.ADDED, "object": m} for m in listed["items"]]
        return events, int(listed["metadata"]["resourceVersion"])

    def _bookmark(self, cursor: int) -> dict:
        kind = COLLECTIONS[self.collection][0]
        return {
            "type": "BOOKMARK",
            "object": {"apiVersion": API_VERSION, "kind": kind, "metadata": {"resourceVersion": str(cursor)}},
        }

    async def produce(self, wakeup: asyncio.Event, put: Callable) -> None:
        """Reads the log until the timeout, handing each event to `put`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        if self.resource_version is None:
            events, cursor = await self.initial()
            for e in events:
                await put(e)
        else:
            cursor = self.resource_version
        next_bookmark = loop.time() + self.bookmark
        while True:
            wakeup.clear()
            try:
                events, cursor = await self.batch(cursor)
            except Gone as e:
                await put(_status(410, "Expired", e.message))
                return
            for e in events:
                await put(e)
            now = loop.time()
            if now >= next_bookmark:
                await put(self._bookmark(cursor))
                next_bookmark = now + self.bookmark
            if now >= deadline:
                return
            if events and len(events) >= BATCH:
                continue
            wait = min(self.poll, next_bookmark - now, deadline - now)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wakeup.wait(), max(wait, 0))


def _status(code: int, reason: str, message: str) -> dict:
    return {
        "type": "ERROR",
        "object": {
            "apiVersion": "v1",
            "kind": "Status",
            "status": "Failure",
            "reason": reason,
            "code": code,
            "message": message,
        },
    }


def timeout_of(requested: int | None) -> float:
    """`timeoutSeconds`, capped; unset is the default plus up to 10% jitter so watches spread out."""
    if requested is None or requested <= 0:
        return DEFAULT_TIMEOUT_SECONDS * (1 + random.random() / 10)  # noqa: S311 - jitter, not security
    return float(min(requested, MAX_TIMEOUT_SECONDS))


async def stream(watch: Watch, hub: Fanout | None = None) -> AsyncIterator[bytes]:
    """The watch as JSON lines. A slot is taken when iteration starts and released when it ends,
    so a stream dropped before it is iterated holds nothing. Callers pre-check with `admit()`; a
    slot lost to a race is an `ERROR` event with a 429 `Status`."""
    hub = hub or fanout()

    async def lines() -> AsyncIterator[bytes]:
        try:
            wakeup = hub.subscribe()
        except TooManyRequests as e:
            yield _line(_status(429, "TooManyRequests", e.message))
            return
        queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        done = object()

        async def put(event: dict) -> None:
            await asyncio.wait_for(queue.put(event), SLOW_SECONDS)

        async def run() -> None:
            try:
                await watch.produce(wakeup, put)
            except TimeoutError:
                logger.info("manifest.watch.slow_client collection=%s", watch.collection)
                return
            except Exception:
                logger.exception("manifest.watch.failed collection=%s", watch.collection)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(_status(500, "InternalError", "the watch failed; relist and watch again"))
            finally:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(done)

        producer = asyncio.create_task(run())
        try:
            while True:
                if producer.done() and queue.empty():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), SLOW_SECONDS)
                except TimeoutError:
                    continue
                if event is done:
                    return
                yield _line(event)
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer
            hub.unsubscribe(wakeup)

    return lines()


def _line(event: dict) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode() + b"\n"
