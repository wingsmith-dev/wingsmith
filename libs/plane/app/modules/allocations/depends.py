from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import AllocationService


async def allocation_svc_generator(context: RequestContext) -> AsyncGenerator[AllocationService, None]:
    yield AllocationService(db=context.db)
