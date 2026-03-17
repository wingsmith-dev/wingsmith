import uuid
import datetime as dt

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.schemas import ListResponse
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import EventSchema
from .schemas import EventSubjectSchema
from .repository import EventRepository


class EventService:
    """Read-only, and deliberately without `create`, `update` or `delete`.

    An event is observed and append-only: it is written by ingestion and never
    by the API. The write methods are absent rather than present-and-raising,
    because a method that exists is a method something will eventually call, and
    the module convention says an observed module exposes no create or update at
    all.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = EventRepository(db)

    def schema(self, row: Row) -> EventSchema:
        event = row[0]
        return EventSchema(
            id=str(event.id),
            cluster_id=row.cluster_uid,
            kind=event.type,
            severity=event.severity,
            subject=EventSubjectSchema(
                type=event.subject_type,
                id=str(event.subject_id) if event.subject_id else None,
            ),
            operation_id=str(event.operation_id) if event.operation_id else None,
            message=event.message,
            details=event.details,
            occurred_at=event.occurred_at,
            recorded_at=event.created_at,
        )

    async def get(self, ref: str) -> EventSchema:
        try:
            uid = uuid.UUID(ref)
        except ValueError as exc:
            raise NotFound(f"no event {ref!r}") from exc
        row = await self.repo.get(uid)
        if row is None:
            raise NotFound(f"no event {ref!r}")
        return self.schema(row)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> ListResponse[EventSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            before_seq=decode_cursor(cursor),
            cluster=cluster,
            subject=subject,
            kind=kind,
            severity=severity,
            since=since,
            until=until,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(r) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)
