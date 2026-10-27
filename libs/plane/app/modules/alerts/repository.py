import datetime as dt

from typing import Any

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Alert
from app.db.models import Cluster
from app.db.models import AlertThreshold


class AlertRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Alert, Cluster.uid.label("cluster_uid")).outerjoin(Cluster, Alert.cluster_id == Cluster.id)

    def get_query(self, ref: str) -> Select:
        return self._base().where(Alert.uid == ref)

    def list_query(
        self,
        limit: int,
        before_seq: int | None = None,
        cluster: str | None = None,
        state: str | None = None,
        severity: str | None = None,
        rule: str | None = None,
        subject: str | None = None,
        since: dt.datetime | None = None,
    ) -> Select:
        """Newest first on `seq`, for the reasons `EventRepository.list_query` gives.

        With one difference that matters: an alert is mutable, so `seq` orders by
        when it was first opened, not by when it last changed. That is the
        wanted behaviour: an alert firing for hours should not keep
        jumping to the top of the list every time the evaluator touches it, and
        `last_seen_at` is returned for anyone who needs recency instead.
        """
        query = self._base().order_by(Alert.seq.desc()).limit(limit + 1)
        if before_seq is not None:
            query = query.where(Alert.seq < before_seq)
        if cluster is not None:
            column = Cluster.uid if is_uid(cluster) else Cluster.name
            query = query.where(column == cluster)
        if state is not None:
            query = query.where(Alert.state == state)
        if severity is not None:
            query = query.where(Alert.severity == severity)
        if rule is not None:
            query = query.where(Alert.rule == rule)
        if subject is not None:
            query = query.where(Alert.subject_uid == subject)
        if since is not None:
            query = query.where(Alert.last_seen_at >= since)
        return query

    async def get(self, ref: str) -> Row | None:
        result = await self.db.execute(self.get_query(ref))
        return result.first()

    async def row(self, ref: str) -> Alert | None:
        result = await self.db.execute(select(Alert).where(Alert.uid == ref))
        return result.scalar_one_or_none()

    async def list(self, **kwargs: Any) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())


class AlertThresholdRepository:
    """Declared state, so unlike `AlertRepository` this one writes. Flushes; never commits."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def get_query(self, ref: str) -> Select:
        return select(AlertThreshold).where(AlertThreshold.uid == ref)

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        rule: str | None = None,
        subject_type: str | None = None,
        subject: str | None = None,
        enabled: bool | None = None,
        fleet_default: bool | None = None,
    ) -> Select:
        """Oldest first on `seq`, the way every declared collection is ordered.

        `fleet_default` filters on `subject_uid IS NULL`, not on a sentinel
        string, because the fleet default is the absence of a subject and a client
        cannot spell that as a value of `subject`.
        """
        query = select(AlertThreshold).order_by(AlertThreshold.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(AlertThreshold.seq > after_seq)
        if rule is not None:
            query = query.where(AlertThreshold.rule == rule)
        if subject_type is not None:
            query = query.where(AlertThreshold.subject_type == subject_type)
        if subject is not None:
            query = query.where(AlertThreshold.subject_uid == subject)
        if enabled is not None:
            query = query.where(AlertThreshold.enabled.is_(enabled))
        if fleet_default is not None:
            query = query.where(
                AlertThreshold.subject_uid.is_(None) if fleet_default else AlertThreshold.subject_uid.is_not(None)
            )
        return query

    async def get(self, ref: str) -> AlertThreshold | None:
        result = await self.db.execute(self.get_query(ref))
        return result.scalar_one_or_none()

    async def list(self, **kwargs: Any) -> list[AlertThreshold]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.scalars().unique())

    async def add(self, threshold: AlertThreshold) -> AlertThreshold:
        self.db.add(threshold)
        await self.db.flush()
        return threshold

    async def delete(self, threshold: AlertThreshold) -> None:
        await self.db.delete(threshold)
        await self.db.flush()
