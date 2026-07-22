from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import GatewayService


async def gateway_svc_generator(context: RequestContext) -> AsyncGenerator[GatewayService, None]:
    yield GatewayService(db=context.db)
