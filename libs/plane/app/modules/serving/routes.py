"""The serving view: what one tenant is being served, and nothing else.

This is the only surface a tenant credential can reach — `app.core.context`
refuses one everywhere else — which makes the module's boundary the security
boundary. Two rules follow, and both are load-bearing rather than tidiness:

Nothing here takes a tenant from the caller. The scope comes off the principal in
`depends`, so no handler can be written that reads the wrong one.

Nothing here writes. A tenant sees declared state somebody else declared and
observed state ingestion wrote; a write on this surface would be a second writer
of both.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide

from app.core.context import RequestContext
from app.modules.metrics.schemas import MetricsSchema
from app.modules.metrics.services import MetricsService

from .depends import serving_svc_generator
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
        """One model as the front door saw it, measured over the tenant's own traffic.

        The reference is resolved against this tenant before the metrics service
        sees it, so what reaches the query is a uid this tenant was proved to own
        rather than a name they typed.
        """
        uid = await svc.metrics_ref(ref)
        metrics = MetricsService(db=context.db)
        return Response(await metrics.endpoint(uid, window, step, span, series))
