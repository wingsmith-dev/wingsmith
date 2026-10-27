"""The tick: hold the lease, evaluate, match, notify. One process at a time, fleet-wide.

The passes of one tick, in this order and for a reason:

0. **Liveness.** Retire hub claims and `reachable` flags that no live session is
   keeping fresh (`app.reactor.liveness`). Before conditions, which read them.
1. **Conditions.** Recompute what is true and reconcile the open condition alerts
   against it. First because it is the pass that can resolve things, and an alert
   that recovered should not be notified as firing in the same tick.
2. **Events.** Drain `ops_event` forward from the cursor, opening an alert for
   anything that matches. Second because an event that arrived during this tick's
   evaluation is better handled on the next one than half-handled on this one.
3. **Notify.** Send what the first two passes made due, and advance the notified
   marker only once every channel has it. Checked against the lease before each
   send, since a slow webhook can outlast it.
4. **Remote-write auth.** Reconcile the central metrics store's htpasswd Secret
   against the registry (`app.reactor.remote_write`). After alerting because it is
   not part of it and must never delay it: it is here for the lease, which is
   the only thing in the plane that guarantees one writer of that Secret.
5. **Repair.** Every few minutes instead of every tick, time out operations past
   their deadline and re-place workloads short of their scale
   (`app.reactor.reconcile`). Last because it is the slowest and the least urgent.

The lease is what makes step 3 safe. Conditions and events are idempotent by
`dedupe_key` and two reactors would merely both write the same rows; two reactors
both sending to Slack is the failure this is here to prevent, so the lease guards
the whole tick, not just the notifier: a process that is not the holder
does no work at all, which is also the cheaper thing for it to do.
"""

import asyncio
import datetime as dt

from logging import getLogger

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.engine import session_factory
from app.db.models import Event
from app.db.models import Cluster
from app.db.models import ReactorState
from app.db.models import NotificationChannel
from app.utils.tasks import cancelled

from . import alerts as alert_store
from . import notify
from . import liveness
from . import reconcile
from . import conditions
from . import remote_write
from .rules import event_intent


logger = getLogger(__name__)

LEASE = "default"

LATE_COMMIT_SECONDS = 600.0
"""How long a seq the cursor stepped over is watched for, in case its transaction commits late."""

MAX_WATCHED_SEQS = 10_000

MAX_GAP = 1_000
"""How far below a seq the read did see a hole is still watched. A transaction that commits late
took its seq at about the same time as rows that are already visible, so it sits just below
one of them; the rest of a wider hole (a reset cursor, a wrapped identity, a large rollback)
is not enumerated, since it would cost more than the events it could find."""


class LateCommits:
    """The seqs the event cursor stepped over without seeing, watched until they show up or expire.

    `ops_event.seq` is assigned when a row is inserted and becomes visible when its
    transaction commits, and the two orders disagree: a frame transaction that took
    seq 50 and committed after another that took 51 is invisible when the reactor
    reads 51 and moves its cursor past both. Read forward from the cursor alone,
    50 is never read at all. Every seq the cursor skips is therefore remembered
    here and looked up again on later ticks; a skipped seq is usually a rolled-back
    transaction and simply expires.

    In memory, because only the lease holder reads it and losing it on a failover
    costs at most the events of one window that also committed late.
    """

    __slots__ = ("_watched", "cap", "window")

    def __init__(self, window: float = LATE_COMMIT_SECONDS, cap: int = MAX_WATCHED_SEQS) -> None:
        self.window = dt.timedelta(seconds=window)
        self.cap = cap
        self._watched: dict[int, dt.datetime] = {}

    def skipped(self, after: int, through: int, seen: set[int], moment: dt.datetime) -> None:
        """Remembers the seqs in `(after, through]` the forward read did not return.

        Hole by hole, between consecutive seqs it did return (and the old cursor), and
        for each only the `MAX_GAP` seqs just below the seq that closes it.
        """
        if through <= after:
            return
        below = after
        for top in sorted(seq for seq in seen if after < seq <= through):
            for seq in range(max(below + 1, top - MAX_GAP), top):
                if len(self._watched) >= self.cap:
                    return
                self._watched.setdefault(seq, moment)
            below = top

    def due(self, moment: dt.datetime) -> list[int]:
        """The seqs still worth looking up, after forgetting the ones watched for a whole window."""
        for seq in [seq for seq, since in self._watched.items() if moment - since > self.window]:
            del self._watched[seq]
        return sorted(self._watched)

    def found(self, seqs: set[int]) -> None:
        for seq in seqs:
            self._watched.pop(seq, None)

    def __len__(self) -> int:
        return len(self._watched)


class DeliveryLedger:
    """Which channels have been told about an alert's current state, and which are backing off.

    `notified_state` on the alert says everything was told; it cannot say who was told
    when only some channels answered. Without this, one failing channel made the next
    tick resend to every healthy channel too, every minute, for as long as the broken
    one stayed broken. A failing channel is retried on a doubling number of ticks, up
    to `MAX_BACKOFF_TICKS`, not on every one.

    In memory, like `LateCommits`: a failover costs at most one repeated message per
    healthy channel for alerts that were mid-delivery.
    """

    __slots__ = ("_backoff", "_told")

    MAX_BACKOFF_TICKS = 32

    def __init__(self) -> None:
        self._told: dict[tuple[str, str], set[str]] = {}
        self._backoff: dict[tuple[str, str, str], tuple[int, int]] = {}

    def told(self, alert: str, state: str, channel: str) -> bool:
        return channel in self._told.get((alert, state), set())

    def record(self, alert: str, state: str, channel: str) -> None:
        self._told.setdefault((alert, state), set()).add(channel)
        self._backoff.pop((alert, state, channel), None)

    def waiting(self, alert: str, state: str, channel: str) -> bool:
        """Whether a channel that failed is still sitting out ticks. Counts this tick off."""
        key = (alert, state, channel)
        entry = self._backoff.get(key)
        if entry is None:
            return False
        failures, skip = entry
        if skip <= 0:
            return False
        self._backoff[key] = (failures, skip - 1)
        return True

    def failed(self, alert: str, state: str, channel: str) -> None:
        key = (alert, state, channel)
        failures = self._backoff.get(key, (0, 0))[0] + 1
        self._backoff[key] = (failures, min(2 ** (failures - 1) - 1, self.MAX_BACKOFF_TICKS))

    def settle(self, alert: str, state: str) -> None:
        """Forgets an alert state once every channel has it."""
        self._told.pop((alert, state), None)
        for key in [key for key in self._backoff if key[:2] == (alert, state)]:
            del self._backoff[key]

    def keep_only(self, current: set[tuple[str, str]]) -> None:
        """Forgets every alert state not in `current`, the ones still waiting to be announced.

        An alert that changed state, or was marked notified another way, while one of its
        channels was failing is never settled; this is what drops it.
        """
        for key in [key for key in self._told if key not in current]:
            del self._told[key]
        for key in [key for key in self._backoff if key[:2] not in current]:
            del self._backoff[key]

    def __len__(self) -> int:
        return len(self._told) + len(self._backoff)


LATE = LateCommits()
LEDGER = DeliveryLedger()


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def ensure_state(db: AsyncSession) -> None:
    """Creates the single lease row if it is not there. Safe to race."""
    await db.execute(
        pg_insert(ReactorState)
        .values(name=LEASE, last_event_seq=0)
        .on_conflict_do_nothing(index_elements=[ReactorState.name])
    )


async def acquire(db: AsyncSession, instance: str, ttl: float) -> int | None:
    """Takes or renews the lease, returning the event cursor, or None if someone else holds it.

    One statement. The `WHERE` is the whole election: a process may take the lease
    if nobody holds it, if it already holds it, or if the current holder's lease
    has expired, and a compare-and-swap over those three cases cannot be observed
    as won by two processes, which a read-then-write over the same cases can.

    An expired lease is taken without ceremony. The displaced holder finds out on
    its next tick, when its own renewal fails, and does nothing further; there is
    no state in memory for it to have to unwind, which is why the cursor lives in
    the row, not in the process.
    """
    result = await db.execute(
        text(
            "UPDATE ops_reactor_state"
            " SET holder = :instance,"
            "     lease_expires_at = now() + make_interval(secs => :ttl),"
            "     last_tick_at = now()"
            " WHERE name = :name"
            "   AND (holder IS NULL OR holder = :instance OR lease_expires_at < now())"
            " RETURNING last_event_seq"
        ),
        {"instance": instance, "ttl": ttl, "name": LEASE},
    )
    row = result.first()
    return None if row is None else row[0]


async def evaluate_conditions(db: AsyncSession, moment: dt.datetime) -> None:
    intents = await conditions.evaluate(db)
    await alert_store.apply_conditions(db, intents, moment)


async def _match(db: AsyncSession, rows, moment: dt.datetime) -> set[int]:
    """Opens an alert for every row that warrants one, and returns the seqs it read."""
    seen: set[int] = set()
    for event, _cluster_uid, cluster_name in rows:
        seen.add(event.seq)
        intent = event_intent(
            event_type=event.type,
            severity=event.severity,
            subject_type=event.subject_type,
            message=event.message,
            details=event.details or {},
            event_uid=event.uid,
            cluster_id=event.cluster_id,
            cluster_name=cluster_name,
        )
        if intent is not None:
            await alert_store.apply_event(db, intent, moment)
    return seen


def _events():
    return select(Event, Cluster.uid, Cluster.name).outerjoin(Cluster, Cluster.id == Event.cluster_id)


async def drain_events(
    db: AsyncSession, cursor: int, batch: int, moment: dt.datetime, late: LateCommits | None = None
) -> int:
    """Matches events from `cursor` forward, returning the new cursor.

    The cursor advances to the last row *examined*, not the last row that produced
    an alert, otherwise a quiet stretch of uninteresting events would be walked
    again on every tick forever. It advances in the same transaction as the alerts
    those events opened, so a crash replays the batch instead of skipping it, and
    `apply_event` is idempotent precisely to make that replay harmless.

    Moving past a seq it did not see is not the same as having read it: the
    transaction holding it may simply not have committed yet. Those seqs are
    handed to `late` and read on the ticks that follow (`LateCommits`).
    """
    late = LATE if late is None else late
    rows = (await db.execute(_events().where(Event.seq > cursor).order_by(Event.seq).limit(batch))).all()
    seen = await _match(db, rows, moment)
    last = max(seen) if seen else cursor
    late.skipped(cursor, last, seen, moment)

    waiting = late.due(moment)
    if waiting:
        found = (await db.execute(_events().where(Event.seq.in_(waiting)).order_by(Event.seq))).all()
        recovered = await _match(db, found, moment)
        late.found(recovered)
        if recovered:
            logger.info("reactor.events.late count=%d", len(recovered))
    if last != cursor:
        await db.execute(
            text("UPDATE ops_reactor_state SET last_event_seq = :seq WHERE name = :name"),
            {"seq": last, "name": LEASE},
        )
    return last


async def holds_lease(db: AsyncSession, instance: str) -> bool:
    """Whether `instance` still holds the lease right now, by the database's clock."""
    held = await db.scalar(
        text("SELECT holder = :instance AND lease_expires_at > now() FROM ops_reactor_state WHERE name = :name"),
        {"instance": instance, "name": LEASE},
    )
    return bool(held)


async def send_pending(
    db: AsyncSession, timeout: float, instance: str | None = None, ledger: DeliveryLedger | None = None
) -> None:
    """Delivers every alert whose state has not been announced, one channel at a time.

    Sequential, not gathered. The volume is a handful of messages per tick
    even during an incident, and a burst of concurrent webhook posts is the fastest
    way to discover Slack's rate limit at exactly the moment the alerts matter.

    Each alert's outcome is committed before the next is sent, and before each send
    the lease is checked again: a slow webhook can take longer than the lease, and
    a process that lost it while sending must not go on sending beside the new
    holder. Delivery is tracked per channel (`DeliveryLedger`), so a channel that
    already took a message is not sent it again because another one failed.
    """
    ledger = LEDGER if ledger is None else ledger
    channels = (await db.execute(select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))).scalars()
    configured = list(channels)
    due = await alert_store.due_for_notice(db)
    ledger.keep_only({(alert.uid, alert.state) for alert in due})
    if not due:
        return
    rows = (await db.execute(select(Cluster.id, Cluster.name))).all()
    names = {row[0]: row[1] for row in rows}

    for alert in due:
        if not alert_store.should_send(alert):
            alert.notified_state = alert.state
            await db.commit()
            continue
        targets = notify.routed_to(configured, alert)
        if not targets:
            alert.notified_state = alert.state
            alert.notified_at = now()
            await db.commit()
            continue
        message = notify.message_for(alert, names.get(alert.cluster_id))
        errors = []
        pending = False
        for channel in targets:
            if ledger.told(alert.uid, alert.state, channel.uid):
                continue
            if ledger.waiting(alert.uid, alert.state, channel.uid):
                pending = True
                continue
            if instance is not None and not await holds_lease(db, instance):
                logger.warning("reactor.notify.lease_lost instance=%s", instance)
                await db.commit()
                return
            try:
                await notify.deliver(channel, message, timeout)
            except notify.DeliveryFailed as error:
                ledger.failed(alert.uid, alert.state, channel.uid)
                errors.append(f"{channel.name}: {error}")
                logger.warning("reactor.notify.failed channel=%s alert=%s error=%s", channel.name, alert.uid, error)
                continue
            ledger.record(alert.uid, alert.state, channel.uid)
        if errors or pending:
            if errors:
                alert.notify_error = "; ".join(errors)
            await db.commit()
            continue
        alert.notified_state = alert.state
        alert.notified_at = now()
        alert.notify_error = None
        ledger.settle(alert.uid, alert.state)
        await db.commit()


async def tick(sessions: async_sessionmaker[AsyncSession], instance: str) -> bool:
    """One full pass. Returns whether this process held the lease and did the work."""
    moment = now()
    async with sessions() as db:
        await ensure_state(db)
        await db.commit()
        cursor = await acquire(db, instance, settings.REACTOR_LEASE_SECONDS)
        if cursor is None:
            await db.rollback()
            return False
        # First, so conditions read reachability as it is, not as a dead hub left it. In a
        # savepoint of its own, so a failure here costs this pass and never the alerting after it.
        try:
            async with db.begin_nested():
                await liveness.sweep(db, moment)
        except Exception:
            logger.warning("reactor.liveness.failed", exc_info=True)
        await evaluate_conditions(db, moment)
        await drain_events(db, cursor, settings.REACTOR_EVENT_BATCH, moment)
        await db.commit()

    async with sessions() as db:
        await send_pending(db, settings.REACTOR_NOTIFY_TIMEOUT, instance)
        await db.commit()

    async with sessions() as db:
        await remote_write.reconcile(db)

    if reconcile.CADENCE.due():
        await _repair(sessions, now())
    return True


async def _repair(sessions: async_sessionmaker[AsyncSession], moment: dt.datetime) -> None:
    """The slow pass (`app.reactor.reconcile`). Each half fails alone and costs nothing else."""
    async with sessions() as db:
        try:
            await reconcile.time_out_operations(db, moment)
        except Exception:
            logger.warning("reactor.reconcile.operations_failed", exc_info=True)
            await db.rollback()
        try:
            await reconcile.replace_short_workloads(db)
        except Exception:
            logger.warning("reactor.reconcile.workloads_failed", exc_info=True)


async def reactor_loop(instance: str, sessions: async_sessionmaker[AsyncSession] | None = None) -> None:
    """Ticks forever. Every failure is logged and slept off, never raised.

    A reactor that exits on the first bad tick is a reactor that is not running
    during the incident that made it exit, which is the only time anyone would
    have wanted it. The work is stateless between ticks by design, so retrying the
    whole thing is always a valid response to any failure within it.
    """
    factory = sessions or session_factory()
    logger.info("reactor.startup instance=%s interval=%s", instance, settings.REACTOR_INTERVAL_SECONDS)
    while True:
        try:
            await tick(factory, instance)
        except asyncio.CancelledError:
            raise
        except Exception:
            if cancelled():
                raise asyncio.CancelledError from None
            logger.warning("reactor.tick.failed instance=%s", instance, exc_info=True)
        await asyncio.sleep(settings.REACTOR_INTERVAL_SECONDS)
