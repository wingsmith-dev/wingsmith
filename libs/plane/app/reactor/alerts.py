"""Turning intents into rows, and deciding which of those rows owes someone a message.

The state machine lives here and nowhere else. Callers hand over what is true
(`app.reactor.conditions`) or what happened (`ops_event`), and this module reconciles
that against what is already written down.

The one rule worth stating outright: **an acknowledgement is never overwritten by
the reactor.** A human saying "I have seen this" must survive every subsequent
evaluation of the same condition, or acknowledging anything is pointless. The
condition going away resolves the alert; the condition continuing does not
un-acknowledge it.
"""

import datetime as dt

from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.ids import ALERT
from app.core.ids import new_uid
from app.db.models import Alert

from . import rules
from .rules import AlertIntent
from .conditions import overdue


logger = getLogger(__name__)

PENDING = "pending"
FIRING = "firing"
ACKNOWLEDGED = "acknowledged"
RESOLVED = "resolved"

OPEN_STATES = (PENDING, FIRING, ACKNOWLEDGED)
STATES = frozenset({PENDING, FIRING, ACKNOWLEDGED, RESOLVED})


def _row(intent: AlertIntent, now: dt.datetime, state: str) -> dict:
    return {
        "uid": new_uid(ALERT),
        "dedupe_key": intent.dedupe_key,
        "rule": intent.rule,
        "source": intent.source,
        "severity": intent.severity,
        "state": state,
        "cluster_id": intent.cluster_id,
        "subject_type": intent.subject_type,
        "subject_uid": intent.subject_uid,
        "title": intent.title,
        "details": intent.details,
        "first_seen_at": now,
        "last_seen_at": now,
        "fired_at": now if state == FIRING else None,
        "occurrences": 1,
    }


async def apply_conditions(db: AsyncSession, intents: list[AlertIntent], now: dt.datetime) -> None:
    """Reconciles the open condition alerts against the set that is currently true.

    Three moves, in one pass: open what is new, promote what has waited long
    enough, and resolve what is no longer in the set. The third is why this takes
    the whole set rather than one intent at a time — auto-resolution is an argument
    from absence, and absence is only meaningful against a complete list.
    """
    by_key = {intent.dedupe_key: intent for intent in intents}
    existing = await db.execute(select(Alert).where(Alert.source == rules.CONDITION))
    rows = {alert.dedupe_key: alert for alert in existing.scalars().all()}

    for key, intent in by_key.items():
        alert = rows.get(key)
        if alert is None:
            state = FIRING if intent.for_seconds <= 0 else PENDING
            db.add(Alert(**_row(intent, now, state)))
            continue
        if alert.state == RESOLVED:
            alert.state = FIRING if intent.for_seconds <= 0 else PENDING
            alert.first_seen_at = now
            alert.fired_at = now if intent.for_seconds <= 0 else None
            alert.resolved_at = None
            alert.acknowledged_at = None
            alert.acknowledged_by = None
            alert.acknowledged_note = None
            alert.notified_state = None
            alert.occurrences += 1
        alert.last_seen_at = now
        alert.severity = intent.severity
        alert.title = intent.title
        alert.details = intent.details
        if alert.state == PENDING and overdue(alert.first_seen_at, now, intent.for_seconds):
            alert.state = FIRING
            alert.fired_at = now

    for key, alert in rows.items():
        if key in by_key or alert.state == RESOLVED:
            continue
        alert.state = RESOLVED
        alert.resolved_at = now
        alert.last_seen_at = now


async def apply_event(db: AsyncSession, intent: AlertIntent, now: dt.datetime) -> None:
    """Opens an alert for one event, or does nothing if that event already has one.

    `ON CONFLICT DO NOTHING` rather than a read-then-insert: the reactor's cursor
    only advances on a committed batch, so a crash mid-batch replays events that
    may already have alerts, and this is the path that has to be idempotent.
    """
    values = _row(intent, now, FIRING)
    await db.execute(pg_insert(Alert).values(**values).on_conflict_do_nothing(index_elements=[Alert.dedupe_key]))


async def due_for_notice(db: AsyncSession) -> list[Alert]:
    """Alerts whose current state has not yet been announced.

    Pending is silent by construction — that is what pending means. Acknowledged
    is silent because someone already knows. What is left is `firing`, and
    `resolved`, which `should_send` then filters further.
    """
    result = await db.execute(
        select(Alert)
        .where(
            Alert.state.in_((FIRING, RESOLVED)),
            Alert.notified_state.is_distinct_from(Alert.state),
        )
        .order_by(Alert.seq)
    )
    return list(result.scalars().all())


def should_send(alert: Alert) -> bool:
    """Whether this state change is worth a message, as opposed to merely worth recording.

    A recovery for an alert nobody was ever told about is the case this exists to
    catch — a condition that flapped from pending to resolved without ever firing.
    Callers still mark it notified, so it is skipped once rather than reconsidered
    on every tick for as long as the row exists.
    """
    if alert.state == RESOLVED:
        return alert.notified_state == FIRING
    return True
