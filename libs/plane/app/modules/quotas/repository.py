from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import SERVING_QUOTA
from app.core.ids import is_uid
from app.db.models import Quota
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Allocation
from app.db.models import Deployment
from app.db.models import ServingQuota
from app.db.models import EndpointTarget
from app.db.models import AllocationDevice
from app.core.lookup import at_most_one
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

    MODEL = Quota

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

    async def get(self, ref: str, lock: bool = False) -> Row | None:
        """`lock` holds the row until the transaction ends, re-read, so an `If-Match` compared under it holds."""
        query = self.get_query(ref).limit(1)
        if lock:
            query = query.with_for_update(of=self.MODEL).execution_options(populate_existing=True)
        result = await self.db.execute(query)
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
        result = await self.db.execute(select(Project).where(Project.id.in_(_project_ids(ref))).limit(2))
        return at_most_one(list(result.scalars()), "project", ref)

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


class ServingQuotaRepository:
    """Reads serving quotas, and nothing about how much traffic they have carried.

    The contrast with `QuotaRepository` above is deliberate and total: that class
    exists largely to count usage, because a device allocation is a row in this
    database. Nothing in here counts anything. A serving quota's consumption is
    measured per request in the gateway and reaches an operator through the
    metrics surface, so a `usage_query` on this class would have to invent its
    numbers or fetch them across the control plane.
    """

    MODEL = ServingQuota

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(
                ServingQuota,
                Endpoint.uid.label("endpoint_uid"),
                Endpoint.name.label("endpoint_name"),
                Project.uid.label("project_uid"),
            )
            .join(Endpoint, ServingQuota.endpoint_id == Endpoint.id)
            .join(Project, Endpoint.project_id == Project.id)
        )

    def get_query(self, ref: str) -> Select:
        """The quota's own id, or the endpoint it commits, by id or by name.

        A serving quota has no name, the same way a device quota does not. The
        endpoint does, and the FK is unique — so naming the endpoint names at
        most one quota, and an `sqt_` id can never be mistaken for an `end_` one.
        """
        if is_uid(ref) and ref.startswith(SERVING_QUOTA):
            return self._base().where(ServingQuota.uid == ref)
        return self._base().where(ServingQuota.endpoint_id.in_(ref_ids(Endpoint, ref)))

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        endpoint: str | None = None,
        project: str | None = None,
        enforced: bool | None = None,
        limit_scope: str | None = None,
    ) -> Select:
        query = self._base().order_by(ServingQuota.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(ServingQuota.seq > after_seq)
        if endpoint is not None:
            query = query.where(ServingQuota.endpoint_id.in_(ref_ids(Endpoint, endpoint)))
        if project is not None:
            query = query.where(Endpoint.project_id.in_(ref_ids(Project, project, name_column="slug")))
        if enforced is not None:
            query = query.where(ServingQuota.enforced.is_(enforced))
        if limit_scope is not None:
            query = query.where(ServingQuota.limit_scope == limit_scope)
        return query

    def workload_endpoints_query(self, ref: str) -> Select:
        """The endpoints a workload's traffic is admitted through.

        Two hops — target to deployment to workload — as a subquery, so resolving
        by workload stays one round trip rather than a fan-out. A workload can sit
        behind more than one endpoint and each of those carries its own
        commitment; this returns the set, and the caller reports them separately.
        """
        return (
            select(EndpointTarget.endpoint_id)
            .join(Deployment, EndpointTarget.deployment_id == Deployment.id)
            .where(Deployment.workload_id.in_(ref_ids(Workload, ref)))
        )

    def resolved_query(self, endpoint: str | None = None, workload: str | None = None) -> Select:
        """Every commitment on an enabled endpoint, with the workloads behind it.

        The outer joins are what make a commitment on an endpoint with no targets
        come back rather than vanish: an endpoint that has been sold a rate and
        has nothing behind it yet is a state an operator has to be able to see,
        and dropping the row would report it as unlimited.
        """
        query = (
            select(
                ServingQuota,
                Endpoint,
                Project.uid.label("project_uid"),
                EndpointTarget,
                Workload,
            )
            .join(Endpoint, ServingQuota.endpoint_id == Endpoint.id)
            .join(Project, Endpoint.project_id == Project.id)
            .outerjoin(
                EndpointTarget,
                and_(EndpointTarget.endpoint_id == Endpoint.id, EndpointTarget.enabled.is_(True)),
            )
            .outerjoin(Deployment, EndpointTarget.deployment_id == Deployment.id)
            .outerjoin(Workload, Deployment.workload_id == Workload.id)
            .where(Endpoint.enabled.is_(True))
            .order_by(ServingQuota.seq.asc())
        )
        if endpoint is not None:
            query = query.where(Endpoint.id.in_(ref_ids(Endpoint, endpoint)))
        if workload is not None:
            query = query.where(Endpoint.id.in_(self.workload_endpoints_query(workload)))
        return query

    async def get(self, ref: str, lock: bool = False) -> Row | None:
        """`lock` holds the row until the transaction ends, re-read, so an `If-Match` compared under it holds."""
        query = self.get_query(ref).limit(1)
        if lock:
            query = query.with_for_update(of=self.MODEL).execution_options(populate_existing=True)
        result = await self.db.execute(query)
        return result.first()

    async def resolved(self, endpoint: str | None = None, workload: str | None = None) -> list[Row]:
        result = await self.db.execute(self.resolved_query(endpoint=endpoint, workload=workload))
        return list(result.all())

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def endpoint_by_ref(self, ref: str) -> Endpoint | None:
        result = await self.db.execute(select(Endpoint).where(Endpoint.id.in_(ref_ids(Endpoint, ref))).limit(1))
        return result.scalar_one_or_none()

    async def add(self, quota: ServingQuota) -> ServingQuota:
        self.db.add(quota)
        await self.db.flush()
        return quota

    async def delete(self, quota: ServingQuota) -> None:
        await self.db.delete(quota)
        await self.db.flush()
