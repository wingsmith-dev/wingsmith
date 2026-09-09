"""Metrics: measurement, read-only, and never mixed into the objects themselves.

These live under `/v1/metrics/...` rather than as a field on a device or a
deployment because the two have different failure modes and different freshness.
An inventory read must never get slower or start failing because a Prometheus is
unreachable, and a client that wants both should ask for both.

There is no `POST` anywhere in this module and there is no state to write.
"""

import datetime as dt

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext

from .sla import PeriodQuery
from .depends import metrics_svc_generator
from .schemas import MetricsSchema
from .schemas import SlaReportSchema
from .schemas import SourceListSchema
from .services import MetricsService


PERIOD_FROM = Parameter(query="from", default=None, required=False)
"""`from` on a report's query string. A Python keyword, so the handler calls it `start`."""

PERIOD_TO = Parameter(query="to", default=None, required=False)


class MetricsRoutes(Controller):
    tags = ["Metrics"]
    path = "/metrics"
    dependencies = {"svc": Provide(metrics_svc_generator)}

    @get("/sources")
    async def list_sources(self, context: RequestContext, svc: MetricsService) -> Response[SourceListSchema]:
        """Which clusters can be measured at all. The first thing to check when a panel is empty."""
        items = await svc.sources()
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

    @get("/clusters/{ref:str}/device-classes/{name:str}")
    async def device_class_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        name: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        """One device class inside one cluster, selected by what the vendor calls it."""
        return Response(await svc.device_class(ref, name, window, step, span, series))

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

    @get("/workloads/{ref:str}")
    async def workload_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        project: str | None = None,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        """One workload across every cluster it landed in, with each cluster in the breakdown.

        `project` disambiguates a bare name, which is unique only within its
        project — the same rule `GET /v1/workloads/{ref}` follows.
        """
        return Response(await svc.workload(ref, project, window, step, span, series))

    @get("/fleet")
    async def fleet_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        """Every cluster at once, in one widened query, with each cluster in the breakdown.

        `unavailable` when the fleet's clusters are not all in one store: the
        answer then belongs to the stores rather than to this process, and a
        merged percentile is not a measurement.
        """
        return Response(await svc.fleet(window, step, span, series))

    @get("/serving")
    async def serving_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        """Everything the gateway answered, with one row per endpoint in the breakdown.

        The fleet total, taken from the hop that saw every request — including the
        ones no cluster ever did. Adding up a page of endpoints instead gives a
        total that changes when somebody pages.
        """
        return Response(await svc.serving(window, step, span, series))

    @get("/tenants/{ref:str}")
    async def tenant_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        """One tenant's traffic at the front door, with one breakdown row per endpoint.

        Here rather than on the tenant object for the reason at the top of this
        module: a tenant read must not start failing because a Prometheus is
        unreachable. `cluster` is null — the gateway belongs to no cluster.
        """
        return Response(await svc.tenant(ref, window, step, span, series))

    @get("/endpoints/{ref:str}")
    async def endpoint_metrics(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> Response[MetricsSchema]:
        """One endpoint, measured at the front door rather than inside a cluster.

        `cluster` is null on the answer: an endpoint is global, and a request that
        was refused before it was routed belongs to no cluster.
        """
        return Response(await svc.endpoint(ref, window, step, span, series))

    @get("/endpoints/{ref:str}/sla")
    async def endpoint_sla(
        self,
        context: RequestContext,
        svc: MetricsService,
        ref: str,
        month: str | None = None,
        start: dt.datetime | None = PERIOD_FROM,
        end: dt.datetime | None = PERIOD_TO,
    ) -> Response[SlaReportSchema]:
        """One endpoint's service level over a period, against the objective its quota declares.

        `month` is `YYYY-MM` in UTC; `from`/`to` are ISO 8601. Neither is the
        current calendar month, measured up to now.
        """
        return Response(await svc.endpoint_sla(ref, PeriodQuery(month=month, start=start, end=end)))

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
