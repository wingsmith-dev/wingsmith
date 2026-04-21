from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import QuotaService
from .services import ServingQuotaService


async def quota_svc_generator(context: RequestContext) -> AsyncGenerator[QuotaService, None]:
    yield QuotaService(db=context.db)


async def serving_quota_svc_generator(context: RequestContext) -> AsyncGenerator[ServingQuotaService, None]:
    yield ServingQuotaService(db=context.db)
