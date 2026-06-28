from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant
from app.db.models import Project
from app.db.models import Endpoint
from app.db.models import Deployment
from app.db.models import EndpointTarget
from app.db.models import DeploymentStatus


class ServingRepository:
    """Reads one tenant's endpoints with their observed readiness summed in.

    Every query here starts at the tenant and joins down. That direction is the
    whole point: a query that started at the endpoint and filtered afterwards
    would return the fleet if the filter were ever dropped, and this one returns
    nothing.

    Readiness is summed across the targets rather than reported per target. Which
    clusters a name is served from, and how the traffic is split between them, is
    ours to change without telling anybody — the tenant is owed how much of their
    model is up, not where.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self, tenant_uid: str) -> Select:
        ready = (
            select(func.coalesce(func.sum(DeploymentStatus.ready_replicas), 0))
            .select_from(EndpointTarget)
            .join(Deployment, Deployment.id == EndpointTarget.deployment_id)
            .join(DeploymentStatus, DeploymentStatus.deployment_id == Deployment.id)
            .where(
                EndpointTarget.endpoint_id == Endpoint.id,
                EndpointTarget.enabled.is_(True),
                Deployment.enabled.is_(True),
            )
            .correlate(Endpoint)
            .scalar_subquery()
        )
        desired = (
            select(func.coalesce(func.sum(Deployment.desired_replicas), 0))
            .select_from(EndpointTarget)
            .join(Deployment, Deployment.id == EndpointTarget.deployment_id)
            .where(
                EndpointTarget.endpoint_id == Endpoint.id,
                EndpointTarget.enabled.is_(True),
                Deployment.enabled.is_(True),
            )
            .correlate(Endpoint)
            .scalar_subquery()
        )
        return (
            select(Endpoint, ready.label("replicas_ready"), desired.label("replicas_desired"))
            .join(Project, Project.id == Endpoint.project_id)
            .join(Tenant, Tenant.id == Project.tenant_id)
            .where(Tenant.uid == tenant_uid)
        )

    async def tenant(self, ref: str) -> Tenant | None:
        """The tenant, by uid or by slug. A disabled one is not returned."""
        query = select(Tenant).where(
            (Tenant.uid == ref) if ref.startswith("ten_") else (Tenant.slug == ref),
            Tenant.enabled.is_(True),
        )
        return (await self.db.execute(query)).scalars().first()

    async def models(self, tenant_uid: str) -> list[Row]:
        result = await self.db.execute(self._base(tenant_uid).order_by(Endpoint.slug.asc()))
        return list(result.all())

    async def model(self, tenant_uid: str, ref: str) -> Row | None:
        query = self._base(tenant_uid).where(
            (Endpoint.uid == ref) if ref.startswith("end_") else (Endpoint.slug == ref)
        )
        return (await self.db.execute(query.limit(1))).first()
