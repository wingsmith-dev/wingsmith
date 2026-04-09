from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import AlertService


async def alert_svc_generator(context: RequestContext) -> AsyncGenerator[AlertService, None]:
    yield AlertService(db=context.db, actor=context.principal.name)
