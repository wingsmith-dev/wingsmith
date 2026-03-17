from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import NodeService


async def node_svc_generator(context: RequestContext) -> AsyncGenerator[NodeService, None]:
    yield NodeService(db=context.db)
