from uuid import UUID
from collections.abc import Sequence

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import AllocationDevice
from app.core.lookup import at_most_one
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

    def get_query(self, ref: str, cluster: str | None = None) -> Select:
        """A node name is unique only inside its cluster, so `cluster` narrows it."""
        column = Node.uid if is_uid(ref) else Node.name
        query = self._base().where(column == ref)
        if cluster is not None:
            query = query.where(Node.cluster_id.in_(ref_ids(Cluster, cluster)))
        return query

    async def get(self, ref: str, cluster: str | None = None) -> Row | None:
        result = await self.db.execute(self.get_query(ref, cluster).limit(2))
        return at_most_one(list(result.all()), "node", ref, "use its id or name a cluster")

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

    def class_rollup_query(self, node_ids: Sequence[UUID]) -> Select:
        return (
            select(Device.node_id, Device.device_class, func.count().label("total"))
            .where(Device.node_id.in_(node_ids))
            .group_by(Device.node_id, Device.device_class)
        )

    async def class_rollup(self, node_ids: Sequence[UUID]) -> dict[UUID, str]:
        """Dominant device class per node, derived from the node's devices.

        The class with the most devices wins; ties break on class name so the
        result is stable. Returned per node so a mixed node still reports the
        model that most of its accelerators are.
        """
        if not node_ids:
            return {}
        result = await self.db.execute(self.class_rollup_query(node_ids))
        counts: dict[UUID, dict[str, int]] = {}
        for node_id, device_class, total in result.all():
            counts.setdefault(node_id, {})[device_class] = total
        return {node_id: max(by_class, key=lambda c: (by_class[c], c)) for node_id, by_class in counts.items()}

    def allocated_query(self, node_ids: Sequence[UUID]) -> Select:
        return (
            select(Device.node_id, func.count().label("allocated"))
            .select_from(AllocationDevice)
            .join(Device, AllocationDevice.device_id == Device.id)
            .where(Device.node_id.in_(node_ids))
            .where(AllocationDevice.released_at.is_(None))
            .group_by(Device.node_id)
        )

    async def allocated_rollup(self, node_ids: Sequence[UUID]) -> dict[UUID, int]:
        """Count of live-allocated devices per node, in one aggregate query.

        Only allocation members with a null `released_at` count, so released
        history never inflates the allocated-of-total figure on the list.
        """
        if not node_ids:
            return {}
        result = await self.db.execute(self.allocated_query(node_ids))
        return {node_id: allocated for node_id, allocated in result.all()}

    def summary_query(self) -> Select:
        return select(
            func.count(Node.id).label("total"),
            func.sum(case((Node.ready.is_(True), 1), else_=0)).label("ready"),
            func.sum(case((Node.cordoned.is_(True), 1), else_=0)).label("cordoned"),
        )

    def faulted_devices_query(self) -> Select:
        return select(func.count(Device.id)).where(Device.health != "healthy")

    async def summary(self) -> tuple[int, int, int, int]:
        """Fleet-wide node counts and faulted-device count, computed server-side.

        Two aggregates instead of a scan of a page: node membership from
        `supply_node`, faulted accelerators from `supply_device` where observed
        health is not `healthy`.
        """
        nodes = (await self.db.execute(self.summary_query())).one()
        faulted = (await self.db.execute(self.faulted_devices_query())).scalar_one()
        return (nodes.total or 0, nodes.ready or 0, nodes.cordoned or 0, faulted or 0)
