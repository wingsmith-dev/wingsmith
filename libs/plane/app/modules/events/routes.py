import datetime as dt

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import event_svc_generator
from .schemas import EventSchema
from .services import EventService


class EventRoutes(Controller):
    """Observed, read-only, append-only: list and detail, and nothing else.

    There is no `POST` here, by design. Events are discovered, not created, and
    per `libs/packages/contract/API.md` §6 writing to an observed resource is a category error
    instead of a validation failure.
    """

    tags = ["Events"]
    path = "/events"
    dependencies = {"svc": Provide(event_svc_generator)}

    @get()
    async def list_events(
        self,
        context: RequestContext,
        svc: EventService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> Response[ListResponse[EventSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            subject=subject,
            kind=kind,
            severity=severity,
            since=since,
            until=until,
        )
        return Response(result)

    @get("/{ref:str}")
    async def get_event(self, context: RequestContext, svc: EventService, ref: str) -> Response[EventSchema]:
        return Response(await svc.get(ref))
