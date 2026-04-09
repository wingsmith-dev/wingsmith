import datetime as dt

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.reactor import alerts as lifecycle
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import AlertSchema
from .schemas import AlertSubjectSchema
from .schemas import AlertAcknowledgeRequest
from .repository import AlertRepository


class AlertService:
    """Read, acknowledge, resolve. Deliberately no create and no delete.

    An alert is derived, so nothing may declare one — that half is the reactor's
    and the module convention would normally make this read-only outright. The
    exception is the acknowledgement, which is the one thing about an alert that
    only a human can know and the reason the row exists rather than a log line.

    No delete, either. Resolving is the way an alert ends; deleting one removes the
    record that it ever happened, which is exactly what nobody wants three weeks
    later when the same thing recurs.
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
