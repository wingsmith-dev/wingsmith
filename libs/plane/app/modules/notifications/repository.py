from typing import Any

from sqlalchemy import Select
from sqlalchemy import ColumnElement
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import NotificationChannel


class NotificationChannelRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _ref_clause(self, ref: str) -> ColumnElement[bool]:
        column = NotificationChannel.uid if is_uid(ref) else NotificationChannel.name
        return column == ref

    def list_query(self, limit: int, after_seq: int | None = None, kind: str | None = None) -> Select:
        query = select(NotificationChannel).order_by(NotificationChannel.seq).limit(limit + 1)
        if after_seq is not None:
            query = query.where(NotificationChannel.seq > after_seq)
        if kind is not None:
            query = query.where(NotificationChannel.kind == kind)
        return query

    async def get(self, ref: str) -> NotificationChannel | None:
        result = await self.db.execute(select(NotificationChannel).where(self._ref_clause(ref)))
        return result.scalar_one_or_none()

    async def list(self, **kwargs: Any) -> list[NotificationChannel]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.scalars().all())

    async def add(self, channel: NotificationChannel) -> NotificationChannel:
        self.db.add(channel)
        await self.db.flush()
        return channel

    async def delete(self, channel: NotificationChannel) -> None:
        await self.db.delete(channel)
        await self.db.flush()
