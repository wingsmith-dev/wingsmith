"""The tick: hold the lease, evaluate, match, notify. One process at a time, fleet-wide.

Three passes per tick, in this order and for a reason:

1. **Conditions.** Recompute what is true and reconcile the open condition alerts
   against it. First because it is the pass that can resolve things, and an alert
   that recovered should not be notified as firing in the same tick.
2. **Events.** Drain `ops_event` forward from the cursor, opening an alert for
   anything that matches. Second because an event that arrived during this tick's
   evaluation is better handled on the next one than half-handled on this one.
3. **Notify.** Send what the first two passes made due, and advance the notified
   marker only on success.
4. **Remote-write auth.** Reconcile the central metrics store's htpasswd Secret
   against the registry (`app.reactor.remote_write`). Last because it is not part
   of alerting at all and must never delay it — it is here for the lease, which is
   the only thing in the plane that guarantees one writer of that Secret.

The lease is what makes step 3 safe. Conditions and events are idempotent by
`dedupe_key` and two reactors would merely both write the same rows; two reactors
both sending to Slack is the failure this is here to prevent, so the lease guards
the whole tick rather than just the notifier — a process that is not the holder
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

from . import alerts as alert_store
from . import notify
from . import conditions
from . import remote_write
from .rules import event_intent


logger = getLogger(__name__)

LEASE = "default"


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
    has expired — and a compare-and-swap over those three cases cannot be observed
    as won by two processes, which a read-then-write over the same cases can.

    An expired lease is taken without ceremony. The displaced holder finds out on
    its next tick, when its own renewal fails, and does nothing further; there is
    no state in memory for it to have to unwind, which is why the cursor lives in
    the row rather than in the process.
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


async def drain_events(db: AsyncSession, cursor: int, batch: int, moment: dt.datetime) -> int:
    """Matches events from `cursor` forward, returning the new cursor.

    The cursor advances to the last row *examined*, not the last row that produced
    an alert — otherwise a quiet stretch of uninteresting events would be walked
    again on every tick forever. It advances in the same transaction as the alerts
    those events opened, so a crash replays the batch rather than skipping it, and
    `apply_event` is idempotent precisely to make that replay harmless.
    """
    rows = await db.execute(
        select(Event, Cluster.uid, Cluster.name)
        .outerjoin(Cluster, Cluster.id == Event.cluster_id)
        .where(Event.seq > cursor)
        .order_by(Event.seq)
        .limit(batch)
    )
    last = cursor
    for event, _cluster_uid, cluster_name in rows.all():
        last = event.seq
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
    if last != cursor:
        await db.execute(
            text("UPDATE ops_reactor_state SET last_event_seq = :seq WHERE name = :name"),
            {"seq": last, "name": LEASE},
        )
    return last


async def send_pending(db: AsyncSession, timeout: float) -> None:
    """Delivers every alert whose state has not been announced, one channel at a time.

    Sequential rather than gathered. The volume is a handful of messages per tick
    even during an incident, and a burst of concurrent webhook posts is the fastest
    way to discover Slack's rate limit at exactly the moment the alerts matter.
    """
    channels = (await db.execute(select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))).scalars()
    configured = list(channels)
    due = await alert_store.due_for_notice(db)
    if not due:
        return
    rows = (await db.execute(select(Cluster.id, Cluster.name))).all()
    names = {row[0]: row[1] for row in rows}

    for alert in due:
        if not alert_store.should_send(alert):
            alert.notified_state = alert.state
            continue
        targets = notify.routed_to(configured, alert)
        if not targets:
            alert.notified_state = alert.state
            alert.notified_at = now()
            continue
        message = notify.message_for(alert, names.get(alert.cluster_id))
        errors = []
        for channel in targets:
            try:
                await notify.deliver(channel, message, timeout)
            except notify.DeliveryFailed as error:
                errors.append(f"{channel.name}: {error}")
                logger.warning("reactor.notify.failed channel=%s alert=%s error=%s", channel.name, alert.uid, error)
        if errors:
            alert.notify_error = "; ".join(errors)
            continue
        alert.notified_state = alert.state
        alert.notified_at = now()
        alert.notify_error = None


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
        await evaluate_conditions(db, moment)
        await drain_events(db, cursor, settings.REACTOR_EVENT_BATCH, moment)
        await db.commit()

    async with sessions() as db:
        await send_pending(db, settings.REACTOR_NOTIFY_TIMEOUT)
        await db.commit()

    async with sessions() as db:
        await remote_write.reconcile(db)
    return True


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
            logger.warning("reactor.tick.failed instance=%s", instance, exc_info=True)
        await asyncio.sleep(settings.REACTOR_INTERVAL_SECONDS)
