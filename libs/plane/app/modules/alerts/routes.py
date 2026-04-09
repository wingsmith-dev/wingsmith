import datetime as dt

from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import alert_svc_generator
from .schemas import AlertSchema
from .schemas import AlertAcknowledgeRequest
from .services import AlertService


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
