from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import ComponentService


async def component_svc_generator(context: RequestContext) -> AsyncGenerator[ComponentService, None]:
    yield ComponentService(db=context.db)
