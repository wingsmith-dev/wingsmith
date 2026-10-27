from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import TopologyDomain
from app.core.filters import ref_ids


class DeviceRepository:
    """Reads only. Devices are discovered, so there is nothing here that writes.

    Every join is explicit and every column is selected up front: the model
    relationships are `lazy="noload"`, and this is the hottest list in the API,
    so a lazy load would be a per-row round trip on the one query that must not
    have them.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(Device, Cluster.uid, Cluster.name, Node.uid, Node.name, TopologyDomain)
            .join(Cluster, Device.cluster_id == Cluster.id)
            .join(Node, Device.node_id == Node.id)
            .outerjoin(TopologyDomain, Device.topology_domain_id == TopologyDomain.id)
        )

    async def get(self, ref: str) -> Row | None:
        column = Device.uid if is_uid(ref) else Device.device_uuid
        result = await self.db.execute(self._base().where(column == ref))
        return result.first()

    def query(
        self,
        limit: int,
        after_seq: int | None = None,
        cluster: str | None = None,
        node: str | None = None,
        pool: str | None = None,
        device_class: str | None = None,
        health: str | None = None,
        allocatable: bool | None = None,
        topology_domain: str | None = None,
    ) -> Select:
        """Builds the list query.

        `cluster`, `device_class` and `health` are applied as equalities on
        `Device` itself so the predicate matches the leading columns of
        `ix_supply_device_class_health`: the composite index is the reason the
        cluster filter is not expressed as a join condition on `Cluster`.
        """
        query = self._base().order_by(Device.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Device.seq > after_seq)
        if cluster is not None:
            query = query.where(Device.cluster_id.in_(ref_ids(Cluster, cluster)))
        if device_class is not None:
            query = query.where(Device.device_class == device_class)
        if health is not None:
            query = query.where(Device.health == health)
        if node is not None:
            query = query.where(Device.node_id.in_(ref_ids(Node, node)))
        if pool is not None:
            query = query.where(Node.pool_id.in_(ref_ids(Pool, pool)))
        if allocatable is not None:
            query = query.where(Device.allocatable == allocatable)
        if topology_domain is not None:
            query = query.where(Device.topology_domain_id.in_(ref_ids_domain(topology_domain)))
        return query

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.query(**kwargs))
        return list(result.all())


def ref_ids_domain(ref: str) -> Select:
    """Topology domains have no unique name, so a non-id reference is the identifier."""
    column = TopologyDomain.uid if is_uid(ref) else TopologyDomain.identifier
    return select(TopologyDomain.id).where(column == ref)
