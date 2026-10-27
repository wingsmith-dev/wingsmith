"""The serving view: what one tenant is being served, and nothing else.

`/v1/serving` is the only surface a tenant credential can reach,
`app.core.context` refuses one everywhere else, which makes the module's
boundary the security boundary. Two rules follow, and both are essential,
not tidiness:

Nothing on `/v1/serving` takes a tenant from the caller. The scope comes off the
principal in `depends`, so no handler can be written that reads the wrong one.

Nothing here writes. A tenant sees declared state somebody else declared and
observed state ingestion wrote; a write on this surface would be a second writer
of both.

An admin reads a customer's view through the second controller, mounted under
`/v1/tenants/{ref}/serving`. It renders the same three reads from the same
service, so the console never grows a second rendering of what a tenant sees.
Which tenant is a path segment there instead of a query parameter on the
tenant-scoped surface, and that is the whole security argument: `/v1/tenants` is
outside `TENANT_SURFACE`, so a tenant credential cannot reach it at all. The
query-parameter shape it replaces could never have worked: the dependency read
`?tenant=`, the handlers never declared it, and the request was refused as an
unknown parameter before the dependency ran.
"""

import datetime as dt

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide

from app.core.context import RequestContext
from app.modules.metrics.sla import PeriodQuery
from app.modules.metrics.routes import PERIOD_TO
from app.modules.metrics.routes import PERIOD_FROM
from app.modules.metrics.schemas import MetricsSchema
from app.modules.metrics.schemas import SlaReportSchema
from app.modules.metrics.services import MetricsService

from .depends import serving_svc_generator
from .depends import tenant_serving_svc_generator
from .schemas import ServingOverviewSchema
from .schemas import ServedModelDetailSchema
from .services import ServingService


class ServingRoutes(Controller):
    tags = ["Serving"]
    path = "/serving"
    dependencies = {"svc": Provide(serving_svc_generator)}

    @get()
    async def overview(self, context: RequestContext, svc: ServingService) -> Response[ServingOverviewSchema]:
        """Every model this tenant is served, with its state. The whole page in one read."""
        return Response(await svc.overview())

    @get("/models/{ref:str}")
    async def model(self, context: RequestContext, svc: ServingService, ref: str) -> Response[ServedModelDetailSchema]:
        return Response(await svc.model(ref))

    @get("/models/{ref:str}/metrics")
    async def model_metrics(
        self,
        context: RequestContext,
        svc: ServingService,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        """One model as the front door's metrics report it, filtered to this tenant.

        The series come from `remote_write`, whose `cluster` label is not
        authenticated (ADR 0012), so these figures are what clusters reported and
        not something the plane measured itself.

        The reference is resolved against this tenant before the metrics service
        sees it, so what reaches the query is a uid this tenant was proved to own
        instead of a name they typed.
        """
        uid = await svc.metrics_ref(ref)
        metrics = MetricsService(db=context.db)
        return Response(await metrics.tenant_endpoint(uid, window, step, span, series))

    @get("/models/{ref:str}/sla")
    async def model_sla(
        self,
        context: RequestContext,
        svc: ServingService,
        ref: str,
        month: str | None = None,
        start: dt.datetime | None = PERIOD_FROM,
        end: dt.datetime | None = PERIOD_TO,
    ) -> Response[SlaReportSchema]:
        """One model's service level over a period, against the objective it was sold under.

        Resolved against this tenant first, as `model_metrics` is, so the report is
        only ever about an endpoint this tenant was proved to own. A report that
        could not be measured says so in one generic line: the operator's reason
        names the infrastructure behind the endpoint.
        """
        uid = await svc.metrics_ref(ref)
        metrics = MetricsService(db=context.db)
        return Response(await metrics.tenant_sla(uid, PeriodQuery(month=month, start=start, end=end)))


class TenantServingRoutes(Controller):
    """A customer's serving view, for somebody who is not that customer.

    Every handler is the tenant-scoped one with a different way of deciding
    whose view it is, so the two cannot drift: same service, same schemas, same
    reads. Only the dependency differs.
    """

    tags = ["Serving"]
    path = "/tenants/{tenant:str}/serving"
    dependencies = {"svc": Provide(tenant_serving_svc_generator)}

    @get()
    async def overview(
        self, context: RequestContext, svc: ServingService, tenant: str
    ) -> Response[ServingOverviewSchema]:
        return Response(await svc.overview())

    @get("/models/{ref:str}")
    async def model(
        self, context: RequestContext, svc: ServingService, tenant: str, ref: str
    ) -> Response[ServedModelDetailSchema]:
        return Response(await svc.model(ref))

    @get("/models/{ref:str}/metrics")
    async def model_metrics(
        self,
        context: RequestContext,
        svc: ServingService,
        tenant: str,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        uid = await svc.metrics_ref(ref)
        metrics = MetricsService(db=context.db)
        return Response(await metrics.tenant_endpoint(uid, window, step, span, series))

    @get("/models/{ref:str}/sla")
    async def model_sla(
        self,
        context: RequestContext,
        svc: ServingService,
        tenant: str,
        ref: str,
        month: str | None = None,
        start: dt.datetime | None = PERIOD_FROM,
        end: dt.datetime | None = PERIOD_TO,
    ) -> Response[SlaReportSchema]:
        uid = await svc.metrics_ref(ref)
        metrics = MetricsService(db=context.db)
        return Response(await metrics.endpoint_sla(uid, PeriodQuery(month=month, start=start, end=end)))
