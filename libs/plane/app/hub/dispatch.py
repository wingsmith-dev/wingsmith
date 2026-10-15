"""Live dispatch of declared state: the hub half of the `app.db.notify` channel.

Every hub process listens, and every hub process sees every notification. One of
them holds the claim on the named cluster (ADR 0005) and therefore has a live
session to push down — two, for at most one ownership poll after a displacement,
and the displaced one's writes are refused — and the rest look the cluster up in
their own in-process index, find nothing, and drop the hint on the floor. That is the whole of the
fan-out design — no routing table, no directory, no second consensus problem
beside the claim table — and it works because a notification is a wakeup rather
than a message, so ignoring one costs nothing.

What gets sent is never the notification's content. The hint names an object; the
renderer re-reads it out of Postgres and sends what the database says now, which
is what keeps live dispatch and a full resync from ever disagreeing about the
same object.
"""

import asyncio

from typing import Any
from typing import Protocol
from logging import getLogger

import psycopg

from msgspec import Struct

from app.db.engine import session_factory
from app.db.notify import CHANNEL
from app.db.notify import OPERATION
from app.db.notify import DEPLOYMENT
from app.db.notify import DeclaredChange
from app.db.notify import decode
from app.db.notify import listen_dsn
from app.hub.record import record_dispatch
from app.ingestion.desired import desired_frames
from app.ingestion.operations import operation_desired


logger = getLogger(__name__)


RETRY_DELAY_SECONDS = 5.0

MAX_PENDING_HINTS = 10_000
"""Hints waiting for dispatch across every cluster this process serves. Past it, a hint is dropped
and the cluster's next periodic resync carries the change instead."""


class Dispatchable(Protocol):
    """The one method dispatch needs off a session. `OperatorSession` satisfies it as-is."""

    async def send_desired(self, type_name: str, payload: Struct, reply_to: str | None = None) -> None: ...


class LiveSessions:
    """The sessions this process can actually push to right now, keyed by `cluster_id`.

    Deliberately a second index rather than a field on `SessionRegistry`. The
    registry holds `SessionState` — cursors and a replay buffer keyed by
    `session_id` — and its whole reason to exist is that it *outlives* the
    connection, so a resumable session sits in it with no socket attached. Live
    dispatch wants the opposite set: the connections that exist this instant, and
    the object that can write to one. Merging them would put a live object with a
    lifetime of one connection inside the structure whose contract is that its
    contents survive connections.

    One session per cluster, because the claim table already guarantees at most
    one anywhere in the fleet. `unregister` is identity-checked so a session
    closing after it was displaced cannot evict the one that displaced it.

    A session is reachable under **every identity its cluster answers to**, not
    only the one the operator introduced itself with. Everywhere else in the
    plane a cluster reference is a uid or a name and `cluster_ref_clause` accepts
    either; this index is the one cluster lookup that is an exact string match,
    and a writer announcing a change holds the uid while an operator configured
    by hand announces a name. Keyed by the handshake identity alone, every
    notification for a cluster that named itself would find no session and be
    dropped in silence, and the cluster would learn of the change only at the
    session's next periodic resync.
    """

    __slots__ = ("_sessions",)

    def __init__(self) -> None:
        self._sessions: dict[str, Dispatchable] = {}

    def register(self, cluster_id: str, session: Dispatchable) -> None:
        self._sessions[cluster_id] = session

    def alias(self, cluster_id: str, session: Dispatchable) -> None:
        """Makes an already-registered session reachable under a second identity.

        Separate from `register` only so the call site reads as what it is. A
        session that arrives later under the same key wins, which is the same
        rule displacement follows everywhere else.
        """
        self._sessions[cluster_id] = session

    def unregister(self, cluster_id: str, session: Dispatchable) -> None:
        """Drops every identity this session is reachable under.

        Identity-checked per key rather than per call: a session that was
        displaced between its own registration and its close must take its own
        keys with it and none of its successor's.
        """
        for key in [key for key, held in self._sessions.items() if held is session]:
            self._sessions.pop(key, None)

    def get(self, cluster_id: str) -> Dispatchable | None:
        return self._sessions.get(cluster_id)

    def __len__(self) -> int:
        return len(self._sessions)


class Renderer(Protocol):
    """Turns a hint back into the frames a resync would have sent for the same object.

    Plural because an object is not always sendable alone: a deployment is
    preceded by the artifact and cache entry it cannot be reconciled without.
    The order is the snapshot's order and the receiver depends on it.
    """

    async def render(self, change: DeclaredChange) -> list[tuple[str, Struct]]: ...


class DatabaseRenderer:
    """Reads the current declared state for one object, in a session of its own.

    Read-only and short-lived: it opens a session per notification, which is the
    right shape for something that fires on writes rather than continuously, and
    keeps the listener's connection — which is doing nothing but `LISTEN` — out of
    the query path entirely.
    """

    __slots__ = ("_factory",)

    def __init__(self, factory: Any | None = None) -> None:
        self._factory = factory

    async def render(self, change: DeclaredChange) -> list[tuple[str, Struct]]:
        factory = self._factory or session_factory()
        async with factory() as db:
            if change.kind == OPERATION:
                one = await operation_desired(db, change.uid)
                return [one] if one else []
            return await desired_frames(db, change.cluster_id, change.kind, change.uid, change.generation)


class Recorder(Protocol):
    """Files the fact that a cluster was told about a change.

    Optional throughout. A hub with no recorder dispatches exactly as before and
    writes nothing, which is what the tests want and what a hub whose database is
    momentarily unwritable should still do.
    """

    async def dispatched(self, change: DeclaredChange, frames: int) -> None: ...


class DatabaseRecorder:
    """Writes a dispatch to the event log, in a session of its own.

    Only deployments. Every kind travels this path, but an artifact or an
    operation reaching a cluster is not a thing anybody is waiting on — a deployment is,
    and "the plane sent it and the cluster has not answered" is the one state
    that used to be indistinguishable from "the plane never sent it".

    Failures are swallowed. This is a record of a send that has already happened;
    losing it must never make the caller think the send did not.
    """

    __slots__ = ("_factory",)

    def __init__(self, factory: Any | None = None) -> None:
        self._factory = factory

    async def dispatched(self, change: DeclaredChange, frames: int) -> None:
        if change.kind != DEPLOYMENT:
            return
        try:
            factory = self._factory or session_factory()
            async with factory() as db:
                await record_dispatch(db, change, frames)
        except Exception:
            logger.warning("hub.dispatch.unrecorded uid=%s", change.uid, exc_info=True)


async def dispatch(
    change: DeclaredChange,
    live: LiveSessions,
    renderer: Renderer,
    recorder: Recorder | None = None,
) -> bool:
    """Sends the current state of one changed object down this process's session for its cluster.

    Returns whether anything was sent. False is the ordinary case on all but one
    hub in the fleet, and is not worth a log line above debug.
    """
    session = live.get(change.cluster_id)
    if session is None:
        return False

    rendered = await renderer.render(change)
    if not rendered:
        logger.debug("hub.dispatch.unrenderable kind=%s uid=%s", change.kind, change.uid)
        return False

    for type_name, payload in rendered:
        await session.send_desired(type_name, payload)
        logger.info("hub.dispatch.sent cluster=%s type=%s uid=%s", change.cluster_id, type_name, change.uid)
    if recorder is not None:
        await recorder.dispatched(change, len(rendered))
    return True


class Dispatcher:
    """Runs dispatch for many clusters at once, one cluster at a time each.

    The listener used to render and send every hint inline, so one cluster whose
    session was slow to drain — or whose render was slow to read — held up every
    other cluster's changes behind it. Each cluster now has a worker of its own,
    started when a hint arrives and gone when its queue is empty, so a cluster's
    hints stay in order and no cluster waits on another's.

    Hints are coalesced per object while they wait. A hint is a wakeup and the
    renderer reads the row as it is when the worker gets to it, so a second hint
    for an object already waiting adds nothing but a second identical send.
    """

    __slots__ = ("_pending", "_queued", "_workers", "live", "max_pending", "recorder", "renderer")

    def __init__(
        self,
        live: LiveSessions,
        renderer: Renderer,
        recorder: Recorder | None = None,
        max_pending: int = MAX_PENDING_HINTS,
    ) -> None:
        self.live = live
        self.renderer = renderer
        self.recorder = recorder
        self.max_pending = max_pending
        self._pending: dict[str, dict[tuple[str, str], DeclaredChange]] = {}
        self._queued = 0
        self._workers: dict[str, asyncio.Task[None]] = {}

    def submit(self, change: DeclaredChange) -> None:
        """Queues one hint. Never blocks and never raises: the listener must keep listening."""
        if self.live.get(change.cluster_id) is None:
            return
        waiting = self._pending.setdefault(change.cluster_id, {})
        key = (change.kind, change.uid)
        if key in waiting:
            waiting[key] = change
            return
        if self._queued >= self.max_pending:
            logger.warning("hub.dispatch.dropped cluster=%s kind=%s uid=%s", change.cluster_id, change.kind, change.uid)
            return
        waiting[key] = change
        self._queued += 1
        if change.cluster_id not in self._workers:
            self._workers[change.cluster_id] = asyncio.create_task(
                self._drain(change.cluster_id), name=f"hub-dispatch-{change.cluster_id}"
            )

    async def _drain(self, cluster_id: str) -> None:
        try:
            while waiting := self._pending.get(cluster_id):
                key = next(iter(waiting))
                change = waiting.pop(key)
                self._queued -= 1
                try:
                    await dispatch(change, self.live, self.renderer, self.recorder)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "hub.dispatch.failed cluster=%s uid=%s", change.cluster_id, change.uid, exc_info=True
                    )
        finally:
            dropped = self._pending.pop(cluster_id, {})
            self._queued -= len(dropped)
            self._workers.pop(cluster_id, None)

    async def close(self) -> None:
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._pending.clear()
        self._queued = 0


async def _connect(dsn: str) -> Any:
    return await psycopg.AsyncConnection.connect(dsn, autocommit=True)


def _handle(payload: str, dispatcher: Dispatcher) -> None:
    """Hands one notification to the dispatcher. Nothing a single bad payload does may end the loop."""
    try:
        change = decode(payload)
    except Exception:  # noqa: BLE001 - a hint nobody can read is a lost wakeup, never a stopped listener
        logger.warning("hub.dispatch.undecodable payload=%r", payload[:256])
        return
    dispatcher.submit(change)


async def listen_loop(
    live: LiveSessions,
    renderer: Renderer | None = None,
    dsn: str | None = None,
    connect: Any | None = None,
    retry_delay: float = RETRY_DELAY_SECONDS,
    recorder: Recorder | None = None,
) -> None:
    """Holds a `LISTEN` for the life of the process, reconnecting for as long as it takes.

    Its own connection, not one out of the pool: a listening connection spends its
    time blocked waiting for notifications, and a pooled session that did that
    would be a session nothing else can use, checked out forever.

    Reconnects are unremarkable for the same reason a dropped notification is.
    Notifications that arrive while this loop is between connections are gone —
    Postgres has nobody to hand them to — and the clusters they concerned keep
    running what they last knew until the next resync corrects them. That is why
    the loop simply waits and reconnects instead of trying to replay the window it
    missed: there is nothing to replay from, and nothing that needs it.
    """
    dispatcher = Dispatcher(
        live,
        DatabaseRenderer() if renderer is None else renderer,
        DatabaseRecorder() if recorder is None else recorder,
    )
    opener = _connect if connect is None else connect
    target = listen_dsn() if dsn is None else dsn

    try:
        while True:
            try:
                connection = await opener(target)
                async with connection:
                    await connection.execute(f"LISTEN {CHANNEL}")
                    logger.info("hub.dispatch.listening channel=%s", CHANNEL)
                    async for note in connection.notifies():
                        _handle(note.payload, dispatcher)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("hub.dispatch.connection_lost", exc_info=True)
            await asyncio.sleep(retry_delay)
    finally:
        await dispatcher.close()
