from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import QuotaService


async def quota_svc_generator(context: RequestContext) -> AsyncGenerator[QuotaService, None]:
    yield QuotaService(db=context.db)
