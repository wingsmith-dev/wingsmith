from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import AlertService
from .services import AlertThresholdService


async def alert_svc_generator(context: RequestContext) -> AsyncGenerator[AlertService, None]:
    yield AlertService(db=context.db, actor=context.principal.name)


async def alert_threshold_svc_generator(context: RequestContext) -> AsyncGenerator[AlertThresholdService, None]:
    yield AlertThresholdService(db=context.db)
