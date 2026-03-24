"""The declared-state change channel: one Postgres `NOTIFY` per write, carrying a hint.

`hangar-api` and `hangar-hub` are two processes that share nothing but Postgres,
so a write to declared state has no way of reaching the cluster it concerns until
the operator next reconnects and is streamed a full resync. This is the wakeup
that closes that gap, and it lives here — beside the engine rather than in either
process — because the writer and the listener are in different binaries and
neither may import the other (see `libs/plane/CLAUDE.md`). The listening half is
`app.hub.dispatch`.

What travels is a **hint**: the cluster, the kind, and the uid of what changed —
never the object. Carrying only a hint is what makes this safe rather than
clever. The receiver re-reads the current row and sends what the database says
now, so Postgres stays the single source of truth and two hints that arrive out
of order cannot deliver stale state between them.

It also means **a lost notification is not a correctness bug**. Notifies are
dropped when nothing is listening, when a hub is mid-restart, when the listening
connection is reconnecting; every one of those leaves the cluster running what it
last knew, and the next `full_resync` — on the operator's own reconnect, which
liveness bounds (PROTOCOL §5) — repairs it. The guarantee is latency, not
delivery, and nothing downstream may assume otherwise.
"""

from typing import Protocol
from logging import getLogger

from msgspec import Struct
from msgspec import json as msgjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


logger = getLogger(__name__)


CHANNEL = "hangar_declared"
"""The one channel. Kinds are a field in the payload rather than a channel each.

A channel per kind would mean every hub issuing a `LISTEN` per kind and another
one every time the object model grows, for no gain: a listener discards by
cluster, not by kind, and a hub holding no session for a cluster pays the same
nothing either way.
"""

DEPLOYMENT = "deployment"


class DeclaredChange(Struct):
    """What a writer says changed. Enough to find the row again, and nothing else.

    `generation` is the one field that is not a lookup key, and it is here only
    for deletes: once the row is gone there is nothing left to read it off, and
    `deployment.deleted` carries one so the operator can order the delete against
    the last desired frame it saw. While the row is still there it is ignored in
    favour of what the database holds.
    """

    cluster_id: str
    kind: str
    uid: str
    generation: int = 0


def encode(change: DeclaredChange) -> str:
    return msgjson.encode(change).decode()


def decode(payload: str) -> DeclaredChange:
    return msgjson.decode(payload.encode(), type=DeclaredChange)


def listen_dsn() -> str:
    """The plain libpq URL. SQLAlchemy's `+psycopg` suffix is not something psycopg parses."""
    return settings.DATABASE_URL.replace("+psycopg", "", 1)


class Notifier(Protocol):
    """The writer's half of the seam. Injected so a service stays testable without a database."""

    async def changed(self, db: AsyncSession, change: DeclaredChange) -> None:
        """Announces that declared state for one object has changed."""
        ...


class DatabaseNotifier:
    """Emits the hint on `CHANNEL`, in a transaction of its own after the writer's.

    The ordering is the whole point and it is deliberate. Postgres queues a
    `pg_notify` until the transaction that issued it commits, so emitting inside
    the write's transaction would be correct too — but only for as long as every
    caller keeps it there, and the first one that does not hands the listener a
    wakeup for a row it cannot see yet, which it answers by dispatching the state
    the write was replacing. Calling this *after* `commit()` makes the ordering
    something visible at the call site rather than something you have to know. The
    cost is one extra round trip on a write path that already did several.

    A failure to notify is logged and swallowed. The write is committed and
    durable by this point; turning a missed wakeup into a failed API call would
    trade seconds of dispatch latency for an error the caller cannot act on, and
    the next resync closes the gap either way.
    """

    async def changed(self, db: AsyncSession, change: DeclaredChange) -> None:
        try:
            await db.execute(
                text("select pg_notify(:channel, :payload)"),
                {"channel": CHANNEL, "payload": encode(change)},
            )
            await db.commit()
        except Exception:
            logger.warning("db.notify.failed cluster=%s uid=%s", change.cluster_id, change.uid, exc_info=True)
            try:
                await db.rollback()
            except Exception:
                logger.debug("db.notify.rollback_failed", exc_info=True)


class MemoryNotifier:
    """Records what would have been announced. For tests and for running with no listener."""

    __slots__ = ("changes",)

    def __init__(self) -> None:
        self.changes: list[DeclaredChange] = []

    async def changed(self, db: AsyncSession, change: DeclaredChange) -> None:
        self.changes.append(change)


class NullNotifier:
    """Announces nothing. Declared state then reaches clusters by resync alone, as it did before."""

    async def changed(self, db: AsyncSession, change: DeclaredChange) -> None: ...
