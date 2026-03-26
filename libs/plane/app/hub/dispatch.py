"""Live dispatch of declared state: the hub half of the `app.db.notify` channel.

Every hub process listens, and every hub process sees every notification. Exactly
one of them holds the claim on the named cluster (ADR 0005) and therefore has a
live session to push down; the rest look the cluster up in their own in-process
index, find nothing, and drop the hint on the floor. That is the whole of the
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
from app.db.notify import DeclaredChange
from app.db.notify import decode
from app.db.notify import listen_dsn
from app.ingestion.desired import desired_object
from app.ingestion.operations import operation_desired


logger = getLogger(__name__)


RETRY_DELAY_SECONDS = 5.0


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
    """

    __slots__ = ("_sessions",)

    def __init__(self) -> None:
        self._sessions: dict[str, Dispatchable] = {}

    def register(self, cluster_id: str, session: Dispatchable) -> None:
        self._sessions[cluster_id] = session

    def unregister(self, cluster_id: str, session: Dispatchable) -> None:
        if self._sessions.get(cluster_id) is session:
            self._sessions.pop(cluster_id, None)

    def get(self, cluster_id: str) -> Dispatchable | None:
        return self._sessions.get(cluster_id)

    def __len__(self) -> int:
        return len(self._sessions)


class Renderer(Protocol):
    """Turns a hint back into the frame a resync would have sent for the same object."""

    async def render(self, change: DeclaredChange) -> tuple[str, Struct] | None: ...


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

    async def render(self, change: DeclaredChange) -> tuple[str, Struct] | None:
        factory = self._factory or session_factory()
        async with factory() as db:
            if change.kind == OPERATION:
                return await operation_desired(db, change.uid)
            return await desired_object(db, change.cluster_id, change.kind, change.uid, change.generation)


async def dispatch(change: DeclaredChange, live: LiveSessions, renderer: Renderer) -> bool:
    """Sends the current state of one changed object down this process's session for its cluster.

    Returns whether anything was sent. False is the ordinary case on all but one
    hub in the fleet, and is not worth a log line above debug.
    """
    session = live.get(change.cluster_id)
    if session is None:
        return False

    rendered = await renderer.render(change)
    if rendered is None:
        logger.debug("hub.dispatch.unrenderable kind=%s uid=%s", change.kind, change.uid)
        return False

    type_name, payload = rendered
    await session.send_desired(type_name, payload)
    logger.info("hub.dispatch.sent cluster=%s type=%s uid=%s", change.cluster_id, type_name, change.uid)
    return True


async def _connect(dsn: str) -> Any:
    return await psycopg.AsyncConnection.connect(dsn, autocommit=True)


async def _handle(payload: str, live: LiveSessions, renderer: Renderer) -> None:
    """Applies one notification. Nothing a single bad payload does may end the loop."""
    try:
        change = decode(payload)
    except Exception:  # noqa: BLE001 - a hint nobody can read is a lost wakeup, never a stopped listener
        logger.warning("hub.dispatch.undecodable payload=%r", payload[:256])
        return
    try:
        await dispatch(change, live, renderer)
    except Exception:
        logger.warning("hub.dispatch.failed cluster=%s uid=%s", change.cluster_id, change.uid, exc_info=True)


async def listen_loop(
    live: LiveSessions,
    renderer: Renderer | None = None,
    dsn: str | None = None,
    connect: Any | None = None,
    retry_delay: float = RETRY_DELAY_SECONDS,
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
    renderer = DatabaseRenderer() if renderer is None else renderer
    opener = _connect if connect is None else connect
    target = listen_dsn() if dsn is None else dsn

    while True:
        try:
            connection = await opener(target)
            async with connection:
                await connection.execute(f"LISTEN {CHANNEL}")
                logger.info("hub.dispatch.listening channel=%s", CHANNEL)
                async for note in connection.notifies():
                    await _handle(note.payload, live, renderer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("hub.dispatch.connection_lost", exc_info=True)
        await asyncio.sleep(retry_delay)
