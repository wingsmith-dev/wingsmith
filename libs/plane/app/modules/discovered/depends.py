from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import DiscoveredWorkloadService


async def discovered_svc_generator(context: RequestContext) -> AsyncGenerator[DiscoveredWorkloadService, None]:
    yield DiscoveredWorkloadService(db=context.db, principal=context.principal.name)
