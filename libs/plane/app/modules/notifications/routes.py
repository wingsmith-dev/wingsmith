from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import patch
from litestar import delete
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import channel_svc_generator
from .schemas import NotificationTestResult
from .schemas import NotificationChannelSchema
from .schemas import NotificationChannelCreateRequest
from .schemas import NotificationChannelUpdateRequest
from .services import NotificationChannelService


class NotificationChannelRoutes(Controller):
    """Where alerts get sent. Declared configuration, one credential, never read back."""

    tags = ["Notifications"]
    path = "/notification-channels"
    dependencies = {"svc": Provide(channel_svc_generator)}

    @get()
    async def list_channels(
        self,
        context: RequestContext,
        svc: NotificationChannelService,
        limit: int | None = None,
        cursor: str | None = None,
        kind: str | None = None,
    ) -> Response[ListResponse[NotificationChannelSchema]]:
        return Response(await svc.list(limit=limit, cursor=cursor, kind=kind))

    @post()
    async def create_channel(
        self, context: RequestContext, svc: NotificationChannelService, data: NotificationChannelCreateRequest
    ) -> Response[NotificationChannelSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_channel(
        self, context: RequestContext, svc: NotificationChannelService, ref: str
    ) -> Response[NotificationChannelSchema]:
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_channel(
        self,
        context: RequestContext,
        svc: NotificationChannelService,
        ref: str,
        data: NotificationChannelUpdateRequest,
    ) -> Response[NotificationChannelSchema]:
        return Response(await svc.update(ref, data))

    @delete("/{ref:str}", status_code=204)
    async def delete_channel(self, context: RequestContext, svc: NotificationChannelService, ref: str) -> None:
        await svc.delete(ref)

    @post("/{ref:str}/test", status_code=200)
    async def test_channel(
        self, context: RequestContext, svc: NotificationChannelService, ref: str
    ) -> Response[NotificationTestResult]:
        return Response(await svc.test(ref))
