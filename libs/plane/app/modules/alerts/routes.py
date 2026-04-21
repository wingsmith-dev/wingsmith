import datetime as dt

from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import patch
from litestar import delete
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import alert_svc_generator
from .depends import alert_threshold_svc_generator
from .schemas import AlertSchema
from .schemas import MetricRuleSchema
from .schemas import AlertThresholdSchema
from .schemas import AlertAcknowledgeRequest
from .schemas import AlertThresholdCreateRequest
from .schemas import AlertThresholdUpdateRequest
from .services import AlertService
from .services import AlertThresholdService
from .services import catalogue


class AlertRoutes(Controller):
    """Derived state: listable, acknowledgeable, resolvable, and not creatable.

    Acknowledge and resolve are `POST` to a sub-path rather than a `PATCH` of
    `state`, because they are transitions with side effects and preconditions, not
    field assignments — the same reason `libs/packages/contract/API.md` §7 keeps operations off the
    resource they act on. There is no `If-Match`: the reactor writes this row on
    every tick, so a revision check would fail constantly and protect nothing an
    acknowledgement can actually clobber.

    `state` is spelled `alert_state` in the handler signature and aliased back onto
    the wire. `state` is reserved by Litestar for the application `State` object, so
    a handler parameter of that name is rejected at registration — and the filter is
    still called `state` in `libs/packages/contract/API.md`, which is the name that has to win.
    """

    tags = ["Alerts"]
    path = "/alerts"
    dependencies = {"svc": Provide(alert_svc_generator)}

    @get()
    async def list_alerts(
        self,
        context: RequestContext,
        svc: AlertService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        alert_state: str | None = Parameter(query="state", default=None, required=False),
        severity: str | None = None,
        rule: str | None = None,
        subject: str | None = None,
        since: dt.datetime | None = None,
    ) -> Response[ListResponse[AlertSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            state=alert_state,
            severity=severity,
            rule=rule,
            subject=subject,
            since=since,
        )
        return Response(result)

    @get("/{ref:str}")
    async def get_alert(self, context: RequestContext, svc: AlertService, ref: str) -> Response[AlertSchema]:
        return Response(await svc.get(ref))

    @post("/{ref:str}/acknowledge")
    async def acknowledge_alert(
        self, context: RequestContext, svc: AlertService, ref: str, data: AlertAcknowledgeRequest
    ) -> Response[AlertSchema]:
        return Response(await svc.acknowledge(ref, data))

    @post("/{ref:str}/resolve")
    async def resolve_alert(self, context: RequestContext, svc: AlertService, ref: str) -> Response[AlertSchema]:
        return Response(await svc.resolve(ref))


class AlertThresholdRoutes(Controller):
    """The declared half of alerting: the numbers, not the alerts they produce.

    A separate resource from `/v1/alerts` rather than a sub-path of it, because a
    threshold is not a property of any alert — it exists before there is one, it
    outlives the object it was written about, and most of them will never produce
    an alert at all. Fully writable, unlike its neighbour.

    No `If-Match`. Nothing but a person writes this row, so the concurrent writer
    a revision check protects against is another human editing the same commitment
    in another tab — worth having eventually, and not worth pretending to have now
    on a resource whose `updated_at` is the only revision there is.
    """

    tags = ["Alerts"]
    path = "/alert-thresholds"
    dependencies = {"svc": Provide(alert_threshold_svc_generator)}

    @get()
    async def list_thresholds(
        self,
        context: RequestContext,
        svc: AlertThresholdService,
        limit: int | None = None,
        cursor: str | None = None,
        rule: str | None = None,
        subject_type: str | None = None,
        subject: str | None = None,
        enabled: bool | None = None,
        fleet_default: bool | None = None,
    ) -> Response[ListResponse[AlertThresholdSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            rule=rule,
            subject_type=subject_type,
            subject=subject,
            enabled=enabled,
            fleet_default=fleet_default,
        )
        return Response(result)

    @post()
    async def create_threshold(
        self, context: RequestContext, svc: AlertThresholdService, data: AlertThresholdCreateRequest
    ) -> Response[AlertThresholdSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_threshold(
        self, context: RequestContext, svc: AlertThresholdService, ref: str
    ) -> Response[AlertThresholdSchema]:
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_threshold(
        self, context: RequestContext, svc: AlertThresholdService, ref: str, data: AlertThresholdUpdateRequest
    ) -> Response[AlertThresholdSchema]:
        return Response(await svc.update(ref, data))

    @delete("/{ref:str}", status_code=204)
    async def delete_threshold(self, context: RequestContext, svc: AlertThresholdService, ref: str) -> None:
        await svc.delete(ref)


class AlertRuleRoutes(Controller):
    """The catalogue of rules a threshold may name. Read-only, and not a database table.

    Its own resource rather than `/v1/alert-thresholds/rules`, so that no literal
    path segment has to win a race against `/{ref}` for a client to discover the
    vocabulary. Which is also why it exists: a console that cannot list the rules
    has to hardcode them, and a hardcoded list is how a metric name becomes a
    string somebody types.
    """

    tags = ["Alerts"]
    path = "/alert-rules"

    @get()
    async def list_rules(self, context: RequestContext) -> Response[ListResponse[MetricRuleSchema]]:
        items = catalogue()
        return Response(ListResponse(items=items, count=len(items)))
