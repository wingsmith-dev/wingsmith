from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Cluster
from app.db.models import ClusterSyncState


class ClusterRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Cluster).options(selectinload(Cluster.sync_state))

    async def get(self, ref: str) -> Cluster | None:
        """Resolves either an id or a name. Prefixes are the client's problem."""
        column = Cluster.uid if is_uid(ref) else Cluster.name
        result = await self.db.execute(self._base().where(column == ref))
        return result.scalar_one_or_none()

    async def list(
        self,
        limit: int,
        after_seq: int | None = None,
        region: str | None = None,
        phase: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[Cluster]:
        query = self._base().order_by(Cluster.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Cluster.seq > after_seq)
        if region is not None:
            query = query.where(Cluster.region == region)
        if phase is not None:
            query = query.where(Cluster.phase == phase)
        if search is not None:
            query = query.where(Cluster.name.ilike(f"%{search}%"))
        for key, value in (labels or {}).items():
            query = query.where(Cluster.labels[key].astext == value)
        result = await self.db.execute(query)
        return list(result.scalars().unique())

    async def add(self, cluster: Cluster) -> Cluster:
        self.db.add(cluster)
        self.db.add(ClusterSyncState(cluster=cluster))
        await self.db.flush()
        return cluster

    async def delete(self, cluster: Cluster) -> None:
        await self.db.delete(cluster)
        await self.db.flush()
