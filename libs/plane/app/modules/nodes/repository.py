from uuid import UUID
from collections.abc import Sequence

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Device
from app.db.models import Cluster
from app.core.filters import ref_ids


class NodeRepository:
    """Reads only. Nodes are written by ingestion and never by the API."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.pool = aliased(Pool)

    def _base(self) -> Select:
        return (
            select(Node, Cluster.uid, Cluster.name, self.pool.uid, self.pool.name)
            .join(Cluster, Node.cluster_id == Cluster.id)
            .outerjoin(self.pool, Node.pool_id == self.pool.id)
        )

    async def get(self, ref: str) -> Row | None:
        column = Node.uid if is_uid(ref) else Node.name
        result = await self.db.execute(self._base().where(column == ref))
        return result.first()

    def query(
        self,
        limit: int,
        after_seq: int | None = None,
        cluster: str | None = None,
        pool: str | None = None,
        condition: str | None = None,
        ready: bool | None = None,
        cordoned: bool | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Select:
        query = self._base().order_by(Node.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Node.seq > after_seq)
        if cluster is not None:
            query = query.where(Node.cluster_id.in_(ref_ids(Cluster, cluster)))
        if pool is not None:
            query = query.where(Node.pool_id.in_(ref_ids(Pool, pool)))
        if condition is not None:
            query = query.where(Node.condition == condition)
        if ready is not None:
            query = query.where(Node.ready == ready)
        if cordoned is not None:
            query = query.where(Node.cordoned == cordoned)
        if search is not None:
            query = query.where(Node.name.ilike(f"%{search}%"))
        for key, value in (labels or {}).items():
            query = query.where(Node.labels[key].astext == value)
        return query

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.query(**kwargs))
        return list(result.all())

    def rollup_query(self, node_ids: Sequence[UUID]) -> Select:
        return (
            select(Device.node_id, Device.health, func.count().label("total"))
            .where(Device.node_id.in_(node_ids))
            .group_by(Device.node_id, Device.health)
        )

    async def device_rollup(self, node_ids: Sequence[UUID]) -> dict[UUID, dict[str, int]]:
        """Device counts per node, by health, in one aggregate query.

        The alternative is a device load per node, which turns a fifty-row page
        into fifty-one queries and makes the list unusable at cluster scale.
        """
        if not node_ids:
            return {}
        result = await self.db.execute(self.rollup_query(node_ids))
        rollup: dict[UUID, dict[str, int]] = {}
        for node_id, health, total in result.all():
            rollup.setdefault(node_id, {})[health] = total
        return rollup
