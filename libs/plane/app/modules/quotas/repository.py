from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Quota
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Allocation
from app.db.models import AllocationDevice
from app.core.filters import ref_ids


def _project_ids(ref: str) -> Select:
    return ref_ids(Project, ref, name_column="slug")


class QuotaRepository:
    """Reads quotas, and counts their usage in the database.

    Usage is an aggregate over live allocations rather than a stored counter.
    A counter would be a second writer on a row the API owns and would drift the
    first time an allocation was released without the plane noticing; a `count`
    over an indexed partial set cannot drift.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(
                Quota,
                Project.uid.label("project_uid"),
                Project.slug.label("project_slug"),
                Cluster.uid.label("cluster_uid"),
                Cluster.name.label("cluster_name"),
            )
            .join(Project, Quota.project_id == Project.id)
            .outerjoin(Cluster, Quota.cluster_id == Cluster.id)
        )

    def get_query(self, ref: str) -> Select:
        """Quotas have no name of their own, so a reference is always the id."""
        return self._base().where(Quota.uid == ref)

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        project: str | None = None,
        cluster: str | None = None,
        device_class: str | None = None,
        over_quota_enabled: bool | None = None,
    ) -> Select:
        query = self._base().order_by(Quota.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Quota.seq > after_seq)
        if project is not None:
            query = query.where(Quota.project_id.in_(_project_ids(project)))
        if cluster is not None:
            query = query.where(Quota.cluster_id.in_(ref_ids(Cluster, cluster)))
        if device_class is not None:
            query = query.where(Quota.device_class == device_class)
        if over_quota_enabled is not None:
            query = query.where(Quota.over_quota_enabled.is_(over_quota_enabled))
        return query

    def usage_query(self, project_ids: list) -> Select:
        """Live device usage per project, cluster and device class.

        The join is restricted to `released_at IS NULL` on both sides: a released
        allocation and a released device row are both history, and counting
        either would report capacity nobody is holding.
        """
        return (
            select(
                Allocation.project_id,
                Allocation.cluster_id,
                Allocation.device_class,
                func.count(AllocationDevice.id).label("devices"),
                func.count(Allocation.id.distinct()).label("allocations"),
                func.sum(case((Allocation.over_quota.is_(True), 1), else_=0)).label("over_quota_devices"),
            )
            .select_from(Allocation)
            .join(
                AllocationDevice,
                and_(
                    AllocationDevice.allocation_id == Allocation.id,
                    AllocationDevice.released_at.is_(None),
                ),
            )
            .where(Allocation.project_id.in_(project_ids), Allocation.released_at.is_(None))
            .group_by(Allocation.project_id, Allocation.cluster_id, Allocation.device_class)
        )

    async def get(self, ref: str) -> Row | None:
        result = await self.db.execute(self.get_query(ref).limit(1))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def usage(self, project_ids: list) -> dict:
        if not project_ids:
            return {}
        result = await self.db.execute(self.usage_query(project_ids))
        return {
            (row.project_id, row.cluster_id, row.device_class): {
                "devices": row.devices or 0,
                "allocations": row.allocations or 0,
                "over_quota_devices": row.over_quota_devices or 0,
            }
            for row in result.all()
        }

    async def project_by_ref(self, ref: str) -> Project | None:
        result = await self.db.execute(select(Project).where(Project.id.in_(_project_ids(ref))).limit(1))
        return result.scalar_one_or_none()

    async def cluster_by_ref(self, ref: str) -> Cluster | None:
        result = await self.db.execute(select(Cluster).where(Cluster.id.in_(ref_ids(Cluster, ref))))
        return result.scalar_one_or_none()

    async def add(self, quota: Quota) -> Quota:
        self.db.add(quota)
        await self.db.flush()
        return quota

    async def delete(self, quota: Quota) -> None:
        await self.db.delete(quota)
        await self.db.flush()
