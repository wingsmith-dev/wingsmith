from uuid import UUID

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Replica
from app.db.models import Allocation
from app.db.models import AllocationDevice
from app.core.filters import ref_ids


def as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def device_ids(ref: str) -> Select:
    """A device is referenced by its prefixed id or by its vendor UUID."""
    return ref_ids(Device, ref, name_column="device_uuid")


class AllocationRepository:
    """Reads only. Allocations are made by placement and reported by the cluster.

    Nothing here writes, and that is structural rather than a convention: the
    partial unique index on `bind_allocation_device.device_id` for live rows is
    what makes a gang atomic, and it can only mean that if exactly one writer —
    ingestion — ever inserts against it.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(
                Allocation,
                Cluster.uid.label("cluster_uid"),
                Cluster.name.label("cluster_name"),
                Project.uid.label("project_uid"),
                Project.slug.label("project_slug"),
                Replica.uid.label("replica_uid"),
            )
            .join(Cluster, Allocation.cluster_id == Cluster.id)
            .join(Project, Allocation.project_id == Project.id)
            .outerjoin(Replica, Allocation.replica_id == Replica.id)
        )

    def get_query(self, ref: str) -> Select:
        """An allocation has no `uid` and no name, so the reference is its key."""
        parsed = as_uuid(ref)
        if parsed is None:
            return self._base().where(Allocation.id.is_(None))
        return self._base().where(Allocation.id == parsed)

    def holder_query(self, device_ref: str) -> Select:
        """Which allocation holds this device right now.

        Answered by the same partial unique index that enforces the rule —
        `device_id WHERE released_at IS NULL` — so it is an index lookup on one
        row rather than a scan of allocation history.
        """
        return select(AllocationDevice.allocation_id).where(
            AllocationDevice.device_id.in_(device_ids(device_ref)),
            AllocationDevice.released_at.is_(None),
        )

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        cluster: str | None = None,
        project: str | None = None,
        device_class: str | None = None,
        phase: str | None = None,
        over_quota: bool | None = None,
        live: bool | None = None,
        replica: str | None = None,
        device: str | None = None,
    ) -> Select:
        query = self._base().order_by(Allocation.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Allocation.seq > after_seq)
        if cluster is not None:
            query = query.where(Allocation.cluster_id.in_(ref_ids(Cluster, cluster)))
        if project is not None:
            query = query.where(Allocation.project_id.in_(ref_ids(Project, project, name_column="slug")))
        if device_class is not None:
            query = query.where(Allocation.device_class == device_class)
        if phase is not None:
            query = query.where(Allocation.phase == phase)
        if over_quota is not None:
            query = query.where(Allocation.over_quota.is_(over_quota))
        if live is not None:
            query = query.where(Allocation.released_at.is_(None) if live else Allocation.released_at.is_not(None))
        if replica is not None:
            query = query.where(Allocation.replica_id.in_(ref_ids(Replica, replica)))
        if device is not None:
            query = query.where(Allocation.id.in_(self.holder_query(device)))
        return query

    def member_query(self, allocation_ids: list) -> Select:
        return (
            select(
                AllocationDevice,
                Device.uid.label("device_uid"),
                Device.device_uuid.label("device_uuid"),
                Node.uid.label("node_uid"),
            )
            .join(Device, AllocationDevice.device_id == Device.id)
            .join(Node, Device.node_id == Node.id)
            .where(AllocationDevice.allocation_id.in_(allocation_ids))
            .order_by(AllocationDevice.seq.asc())
        )

    async def get(self, ref: str) -> Row | None:
        result = await self.db.execute(self.get_query(ref).limit(1))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def members(self, allocation_ids: list) -> dict:
        """Every allocation's devices in one grouped query, keyed by allocation."""
        grouped: dict = {}
        if not allocation_ids:
            return grouped
        result = await self.db.execute(self.member_query(allocation_ids))
        for row in result.all():
            grouped.setdefault(row[0].allocation_id, []).append(row)
        return grouped
