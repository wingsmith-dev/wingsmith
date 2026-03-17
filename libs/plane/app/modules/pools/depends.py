from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import PoolService


async def pool_svc_generator(context: RequestContext) -> AsyncGenerator[PoolService, None]:
    yield PoolService(db=context.db)
