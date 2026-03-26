from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import PlacementService


async def placement_svc_generator(context: RequestContext) -> AsyncGenerator[PlacementService, None]:
    yield PlacementService(db=context.db)
