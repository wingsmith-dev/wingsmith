from sqlalchemy import Row
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant
from app.db.models import Endpoint
from app.db.models import ServingQuota
from app.core.exceptions import NotFound
from app.core.exceptions import NotAuthorized

from .schemas import ServedModelSchema
from .schemas import ServingLimitsSchema
from .schemas import ServingOverviewSchema
from .schemas import ServedModelDetailSchema
from .repository import ServingRepository


def limits_of(quota: ServingQuota | None) -> ServingLimitsSchema | None:
    """The terms a tenant's model is served under, as the tenant may read them.

    `enforced` is false whatever the operator set: the gateway does not read serving
    quotas yet, and telling a tenant a limit is enforced when no request is ever
    refused over it would be a promise nothing keeps.
    """
    if quota is None:
        return None
    return ServingLimitsSchema(
        requests_per_minute=quota.committed_rpm,
        tokens_per_minute=quota.committed_tpm,
        max_concurrent_requests=quota.max_concurrent_requests,
        enforced=False,
    )


class ServingService:
    """The read-only view one tenant has of what we serve them.

    Read-only in the strict sense the module guide means: there are no write
    methods here, not write methods that refuse. Everything this reports is
    declared somewhere else or observed by ingestion, so a write here would be a
    second writer of somebody else's state.

    The service takes the tenant it is scoped to at construction and never as an
    argument. A `tenant_uid` parameter on each method is a parameter somebody can
    forget to pass, and forgetting it here means answering with the fleet.
    """

    def __init__(self, db: AsyncSession, tenant_uid: str) -> None:
        self.db = db
        self.tenant_uid = tenant_uid
        self.repo = ServingRepository(db)

    @staticmethod
    def _state(endpoint: Endpoint, ready: int) -> str:
        """Whether a name is answering, and if not, whether that was deliberate.

        `paused` is checked first and beats readiness. A model somebody turned off
        has no ready replicas by construction, and reporting that as `degraded`
        would page somebody about a decision we made.
        """
        if not endpoint.enabled:
            return "paused"
        return "serving" if ready > 0 else "degraded"

    def _model(self, row: Row) -> ServedModelSchema:
        endpoint = row[0]
        return ServedModelSchema(
            id=endpoint.uid,
            name=endpoint.name,
            model=endpoint.slug or endpoint.name,
            state=self._state(endpoint, row.replicas_ready),
            replicas_ready=row.replicas_ready,
            replicas_desired=row.replicas_desired,
            created_at=endpoint.created_at,
        )

    async def _tenant(self) -> Tenant:
        tenant = await self.repo.tenant(self.tenant_uid)
        if tenant is None:
            raise NotAuthorized("this tenant is not available")
        return tenant

    async def overview(self) -> ServingOverviewSchema:
        """Every model this tenant is served, in one read."""
        tenant = await self._tenant()
        rows = await self.repo.models(tenant.uid)
        return ServingOverviewSchema(
            tenant=tenant.slug,
            tenant_name=tenant.name,
            models=[self._model(row) for row in rows],
        )

    async def model(self, ref: str) -> ServedModelDetailSchema:
        """One model, with the terms it is served under.

        A model belonging to another tenant is a `404` and not a `403`. The two
        answers differ in what they tell a caller who guessed a name: one says
        "not yours", which confirms it exists.
        """
        tenant = await self._tenant()
        row = await self.repo.model(tenant.uid, ref)
        if row is None:
            raise NotFound(f"no model {ref!r}")
        endpoint = row[0]
        quota = (
            (await self.db.execute(select(ServingQuota).where(ServingQuota.endpoint_id == endpoint.id)))
            .scalars()
            .first()
        )
        return ServedModelDetailSchema(
            model=self._model(row),
            limits=limits_of(quota),
            protocols=list(endpoint.surface.get("protocols") or []),
        )

    async def metrics_ref(self, ref: str) -> str:
        """The endpoint uid behind a model reference, if it belongs to this tenant.

        The metrics service takes an endpoint reference and knows nothing about
        tenants. Resolving the reference here first is what stops a tenant reading
        another tenant's traffic by naming their endpoint: the metrics query is
        given a uid this tenant was proved to own, never the string they sent.

        The tenant is resolved before the endpoint for the same reason `model`
        does it: an admin looking at a customer may name them by slug, and the
        endpoint query joins on the uid.
        """
        tenant = await self._tenant()
        row = await self.repo.model(tenant.uid, ref)
        if row is None:
            raise NotFound(f"no model {ref!r}")
        return row[0].uid
