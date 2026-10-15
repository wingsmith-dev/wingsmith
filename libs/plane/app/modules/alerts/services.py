import re
import datetime as dt

from msgspec import UNSET
from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.reactor import rules
from app.reactor import alerts as lifecycle
from app.core.ids import ALERT_THRESHOLD
from app.core.ids import is_uid
from app.core.ids import new_uid
from app.db.models import AlertThreshold
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.metrics.registry import BY_NAME

from .schemas import AlertSchema
from .schemas import MetricRuleSchema
from .schemas import AlertSubjectSchema
from .schemas import AlertThresholdSchema
from .schemas import AlertAcknowledgeRequest
from .schemas import AlertThresholdCreateRequest
from .schemas import AlertThresholdUpdateRequest
from .repository import AlertRepository
from .repository import AlertThresholdRepository


class AlertService:
    """Read, acknowledge, resolve. Deliberately no create and no delete.

    Alerts are derived by the reactor, so nothing declares one; acknowledgement is the one
    human input. Resolving is how an alert ends, and there is no delete, so the record of an
    alert outlives it.
    """

    def __init__(self, db: AsyncSession, actor: str = "unknown") -> None:
        self.db = db
        self.actor = actor
        self.repo = AlertRepository(db)

    def schema(self, row: Row) -> AlertSchema:
        alert = row[0]
        return AlertSchema(
            id=alert.uid,
            rule=alert.rule,
            source=alert.source,
            severity=alert.severity,
            state=alert.state,
            title=alert.title,
            cluster_id=row.cluster_uid,
            subject=AlertSubjectSchema(type=alert.subject_type, id=alert.subject_uid),
            details=alert.details,
            occurrences=alert.occurrences,
            first_seen_at=alert.first_seen_at,
            last_seen_at=alert.last_seen_at,
            fired_at=alert.fired_at,
            resolved_at=alert.resolved_at,
            acknowledged_at=alert.acknowledged_at,
            acknowledged_by=alert.acknowledged_by,
            acknowledged_note=alert.acknowledged_note,
            notified_at=alert.notified_at,
            notify_error=alert.notify_error,
        )

    async def get(self, ref: str) -> AlertSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no alert {ref!r}")
        return self.schema(row)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        state: str | None = None,
        severity: str | None = None,
        rule: str | None = None,
        subject: str | None = None,
        since: dt.datetime | None = None,
    ) -> ListResponse[AlertSchema]:
        if state is not None and state not in lifecycle.STATES:
            raise Invalid(f"unknown state {state!r}", {"allowed": sorted(lifecycle.STATES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            before_seq=decode_cursor(cursor),
            cluster=cluster,
            state=state,
            severity=severity,
            rule=rule,
            subject=subject,
            since=since,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(r) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def acknowledge(self, ref: str, data: AlertAcknowledgeRequest) -> AlertSchema:
        """Silences an alert without claiming it is over.

        Refused on a resolved alert, because acknowledging one would reopen a
        closed record to say something about it that its history already says
        better. Allowed on a pending one: someone watching a condition come true
        should not have to wait for the `for` window to elapse before they can mark
        that they are on it.
        """
        alert = await self.repo.row(ref)
        if alert is None:
            raise NotFound(f"no alert {ref!r}")
        if alert.state == lifecycle.RESOLVED:
            raise Invalid("this alert is already resolved", {"alert": ref, "state": alert.state})
        alert.state = lifecycle.ACKNOWLEDGED
        alert.acknowledged_at = dt.datetime.now(dt.UTC)
        alert.acknowledged_by = self.actor
        alert.acknowledged_note = data.note
        await self.db.commit()
        return await self.get(ref)

    async def resolve(self, ref: str) -> AlertSchema:
        """Closes an alert by hand.

        The honest path for an event-sourced alert, which has no level to fall back
        to and would otherwise stay open forever. Allowed on a condition-sourced one
        too, and worth knowing what that means: the evaluator recomputes from scratch
        every tick, so if the condition is still true the alert reopens on the next
        one, with `occurrences` incremented. Resolving something that is still broken
        is a snooze, and a short one.
        """
        alert = await self.repo.row(ref)
        if alert is None:
            raise NotFound(f"no alert {ref!r}")
        if alert.state == lifecycle.RESOLVED:
            return await self.get(ref)
        alert.state = lifecycle.RESOLVED
        alert.resolved_at = dt.datetime.now(dt.UTC)
        await self.db.commit()
        return await self.get(ref)


LOOKBACK = re.compile(r"^[1-9][0-9]*[smhd]$")
"""A Prometheus-shaped window. Not a duration in general: `0s` and `1w` are neither."""


def catalogue() -> list[MetricRuleSchema]:
    """Every rule a threshold may name, with the unit the number has to be written in.

    Derived from `app.reactor.rules` and the metrics registry, never stored. A
    second copy of this list in the database would be a second thing to keep in
    step with the predicates that are actually compiled in.
    """
    return [
        MetricRuleSchema(
            name=definition.rule,
            metric=definition.metric,
            unit=BY_NAME[definition.metric].unit if definition.metric in BY_NAME else "",
            comparison=definition.comparison,
            subject_type=definition.subject_type,
            severity=definition.severity,
            measures=definition.noun,
            suggested_value=definition.suggested,
            lookback=definition.lookback,
            for_seconds=definition.for_seconds,
        )
        for definition in sorted(rules.METRIC_RULES.values(), key=lambda d: d.rule)
    ]


def _definition(name: str) -> rules.MetricRule:
    if name not in rules.METRIC_RULES:
        raise Invalid(f"unknown rule {name!r}", {"allowed": sorted(rules.METRIC_RULES)})
    return rules.METRIC_RULES[name]


def _lookback(value: str) -> str:
    if not LOOKBACK.match(value):
        raise Invalid(f"lookback {value!r} is not a window", {"example": "10m"})
    return value


def _severity(value: str) -> str:
    if value not in rules.SEVERITY_ORDER:
        raise Invalid(f"unknown severity {value!r}", {"allowed": sorted(rules.SEVERITY_ORDER)})
    return value


def _dimensions(value: dict) -> dict:
    """Narrowing labels, flat and stringly typed.

    Rejected rather than coerced, because `dimensions` is part of the row's
    identity under `uq_ops_alert_threshold_scope` and `1` and `"1"` are two rows
    to Postgres and one commitment to the person who wrote them.
    """
    if not isinstance(value, dict):
        raise Invalid("dimensions must be an object")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise Invalid("dimensions must be a flat object of strings", {"key": str(key)})
    return value


def _for_seconds(value: int) -> int:
    if value < 0:
        raise Invalid("for_seconds cannot be negative", {"for_seconds": value})
    return value


class AlertThresholdService:
    """Declared state, fully writable — the half of alerting a person owns.

    The opposite of `AlertService` next door, and deliberately so: an alert is
    derived and cannot be created, a threshold is a commitment and cannot be
    anything else. The number, the window and the scope are declared here; the
    predicate that watches them is compiled into `app/reactor/rules.py` and is
    named, not written, by whoever writes a row. That is the line that keeps the
    reactor's judgement in reviewed code and its external reads off a
    user-supplied selector.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = AlertThresholdRepository(db)

    def schema(self, row: AlertThreshold) -> AlertThresholdSchema:
        definition = rules.METRIC_RULES.get(row.rule)
        return AlertThresholdSchema(
            id=row.uid,
            rule=row.rule,
            metric=definition.metric if definition else "",
            comparison=definition.comparison if definition else "",
            subject=AlertSubjectSchema(type=row.subject_type, id=row.subject_uid),
            dimensions=row.dimensions,
            value=float(row.value),
            objective=row.objective,
            lookback=row.lookback,
            for_seconds=row.for_seconds,
            severity=row.severity,
            enabled=row.enabled,
            reason=row.reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def get(self, ref: str) -> AlertThresholdSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no alert threshold {ref!r}")
        return self.schema(row)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        rule: str | None = None,
        subject_type: str | None = None,
        subject: str | None = None,
        enabled: bool | None = None,
        fleet_default: bool | None = None,
    ) -> ListResponse[AlertThresholdSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            rule=rule,
            subject_type=subject_type,
            subject=subject,
            enabled=enabled,
            fleet_default=fleet_default,
        )
        kept, next_cursor = page(rows, size, lambda r: r.seq)
        items = [self.schema(r) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: AlertThresholdCreateRequest) -> AlertThresholdSchema:
        """Writes one commitment, with the rule checked at write time rather than at evaluation.

        A row naming a rule nothing evaluates is not a harmless row: it reads as a
        promise being watched, and the first anyone learns otherwise is that the
        alert never came.

        `subject_type` may be omitted, because a rule already knows what kind of
        thing it is about. Supplying one that disagrees is refused rather than
        silently corrected — the disagreement means the writer believed something
        false about the rule.
        """
        definition = _definition(data.rule)
        subject_type = data.subject_type or definition.subject_type
        if subject_type != definition.subject_type:
            raise Invalid(
                f"rule {data.rule!r} is about a {definition.subject_type}, not a {subject_type}",
                {"subject_type": definition.subject_type},
            )
        if data.subject_id is not None and not is_uid(data.subject_id):
            raise Invalid(f"subject id {data.subject_id!r} is not an id", {"example": "dep_01j9x2..."})
        threshold = AlertThreshold(
            uid=new_uid(ALERT_THRESHOLD),
            rule=data.rule,
            subject_type=subject_type,
            subject_uid=data.subject_id,
            dimensions=_dimensions(data.dimensions),
            value=data.value,
            objective=data.objective,
            lookback=_lookback(data.lookback) if data.lookback is not None else None,
            for_seconds=_for_seconds(data.for_seconds) if data.for_seconds is not None else None,
            severity=_severity(data.severity) if data.severity is not None else None,
            enabled=data.enabled,
            reason=data.reason,
        )
        try:
            await self.repo.add(threshold)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            scope = data.subject_id or "the fleet"
            raise Conflict(f"a threshold for {data.rule!r} on {scope} already exists") from exc
        return await self.get(threshold.uid)

    async def update(self, ref: str, data: AlertThresholdUpdateRequest) -> AlertThresholdSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no alert threshold {ref!r}")
        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if fields.get("lookback") is not None:
            _lookback(fields["lookback"])
        if fields.get("severity") is not None:
            _severity(fields["severity"])
        if fields.get("for_seconds") is not None:
            _for_seconds(fields["for_seconds"])
        if "dimensions" in fields:
            _dimensions(fields["dimensions"])
        for key, value in fields.items():
            setattr(row, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("that threshold conflicts with an existing one") from exc
        return await self.get(row.uid)

    async def delete(self, ref: str) -> None:
        """Removes the commitment. Turning one off without losing it is `enabled: false`."""
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no alert threshold {ref!r}")
        await self.repo.delete(row)
        await self.db.commit()
