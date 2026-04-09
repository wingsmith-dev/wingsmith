from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import TenantService


async def tenant_svc_generator(context: RequestContext) -> AsyncGenerator[TenantService, None]:
    yield TenantService(db=context.db)
