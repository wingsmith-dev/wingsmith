from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import NotificationChannelService


async def channel_svc_generator(context: RequestContext) -> AsyncGenerator[NotificationChannelService, None]:
    yield NotificationChannelService(db=context.db)
