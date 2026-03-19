from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import MetricsService


async def metrics_svc_generator(context: RequestContext) -> AsyncGenerator[MetricsService, None]:
    yield MetricsService(db=context.db)
