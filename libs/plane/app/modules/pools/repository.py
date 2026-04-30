from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Device
from app.db.models import Cluster
from app.db.supply import free_device_sum


def _cluster_column(ref: str):
    return Cluster.uid if is_uid(ref) else Cluster.name


class PoolRepository:
    """Reads pools and rolls their membership up in aggregate.

    Membership is counted in the database rather than by walking
    `Pool.nodes` and `Node.devices`: those relationships are `lazy="noload"`, and
    even if they were not, a pool of three hundred eight-GPU nodes would mean
    loading two and a half thousand rows to answer a question that is six
    integers.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Pool, Cluster.uid.label("cluster_uid")).join(Cluster, Pool.cluster_id == Cluster.id)

    def node_rollup_query(self, pool_ids: list) -> Select:
        return (
            select(
                Node.pool_id,
                func.count(Node.id).label("nodes_total"),
                func.sum(case((Node.ready.is_(True), 1), else_=0)).label("nodes_ready"),
                func.sum(case((Node.cordoned.is_(True), 1), else_=0)).label("nodes_cordoned"),
            )
            .where(Node.pool_id.in_(pool_ids))
            .group_by(Node.pool_id)
        )

    def device_rollup_query(self, pool_ids: list) -> Select:
        return (
            select(
                Node.pool_id,
                func.count(Device.id).label("devices_total"),
                func.sum(case((Device.health == "healthy", 1), else_=0)).label("devices_healthy"),
                free_device_sum().label("devices_allocatable"),
            )
            .select_from(Device)
            .join(Node, Device.node_id == Node.id)
            .where(Node.pool_id.in_(pool_ids))
            .group_by(Node.pool_id)
        )

    def get_query(self, ref: str, cluster: str | None = None) -> Select:
        column = Pool.uid if is_uid(ref) else Pool.name
        query = self._base().where(column == ref)
        if cluster is not None:
            query = query.where(_cluster_column(cluster) == cluster)
        return query

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        cluster: str | None = None,
        device_class: str | None = None,
        enabled: bool | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Select:
        query = self._base().order_by(Pool.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Pool.seq > after_seq)
        if cluster is not None:
            query = query.where(_cluster_column(cluster) == cluster)
        if device_class is not None:
            query = query.where(Pool.device_class == device_class)
        if enabled is not None:
            query = query.where(Pool.enabled.is_(enabled))
        if search is not None:
            query = query.where(Pool.name.ilike(f"%{search}%"))
        for key, value in (labels or {}).items():
            query = query.where(Pool.labels[key].astext == value)
        return query

    async def get(self, ref: str, cluster: str | None = None) -> Row | None:
        """Resolves either an id or a name.

        A pool name is unique only within its cluster, so a bare name is
        ambiguous across clusters; `cluster` narrows it, and without it the first
        match wins the same way it does for any non-unique lookup.
        """
        result = await self.db.execute(self.get_query(ref, cluster).limit(1))
        return result.first()

    async def cluster_by_ref(self, ref: str) -> Cluster | None:
        result = await self.db.execute(select(Cluster).where(_cluster_column(ref) == ref))
        return result.scalar_one_or_none()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def membership(self, pool_ids: list) -> dict:
        """Two grouped queries, one keyed dict. Empty pools are simply absent."""
        rollup: dict = {}
        if not pool_ids:
            return rollup
        nodes = await self.db.execute(self.node_rollup_query(pool_ids))
        for row in nodes.all():
            rollup[row.pool_id] = {
                "nodes_total": row.nodes_total or 0,
                "nodes_ready": row.nodes_ready or 0,
                "nodes_cordoned": row.nodes_cordoned or 0,
            }
        devices = await self.db.execute(self.device_rollup_query(pool_ids))
        for row in devices.all():
            rollup.setdefault(row.pool_id, {}).update(
                devices_total=row.devices_total or 0,
                devices_healthy=row.devices_healthy or 0,
                devices_allocatable=row.devices_allocatable or 0,
            )
        return rollup

    async def add(self, pool: Pool) -> Pool:
        self.db.add(pool)
        await self.db.flush()
        return pool

    async def delete(self, pool: Pool) -> None:
        await self.db.delete(pool)
        await self.db.flush()
