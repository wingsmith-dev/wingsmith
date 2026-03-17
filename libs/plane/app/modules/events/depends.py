from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import EventService


async def event_svc_generator(context: RequestContext) -> AsyncGenerator[EventService, None]:
    yield EventService(db=context.db)
