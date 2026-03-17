from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import EndpointService


async def endpoint_svc_generator(context: RequestContext) -> AsyncGenerator[EndpointService, None]:
    yield EndpointService(db=context.db)
