"""Metrics: measurement, read-only, and never mixed into the objects themselves.

These live under `/v1/metrics/...` rather than as a field on a device or a
deployment because the two have different failure modes and different freshness.
An inventory read must never get slower or start failing because a Prometheus is
unreachable, and a client that wants both should ask for both.

There is no `POST` anywhere in this module and there is no state to write.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide

from app.core.context import RequestContext

from .depends import metrics_svc_generator
from .schemas import MetricsSchema
from .schemas import SourceListSchema
from .services import MetricsService


class MetricsRoutes(Controller):
    tags = ["Metrics"]
    path = "/metrics"
    dependencies = {"svc": Provide(metrics_svc_generator)}

    @get("/sources")
    async def list_sources(self, context: RequestContext, svc: MetricsService) -> Response[SourceListSchema]:
        """Which clusters can be measured at all. The first thing to check when a panel is empty."""
        items = svc.sources()
        return Response(SourceListSchema(items=items, count=len(items)))

    @get("/clusters/{ref:str}")
    async def cluster_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        return Response(await svc.cluster(ref, window, step, span, series))

    @get("/pools/{ref:str}")
    async def pool_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        return Response(await svc.pool(ref, window, step, span, series))

    @get("/nodes/{ref:str}")
    async def node_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        return Response(await svc.node(ref, window, step, span, series))

    @get("/devices/{ref:str}")
    async def device_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        return Response(await svc.device(ref, window, step, span, series))

    @get("/deployments/{ref:str}")
    async def deployment_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        return Response(await svc.deployment(ref, window, step, span, series))
