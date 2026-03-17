from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import ClusterService


async def cluster_svc_generator(context: RequestContext) -> AsyncGenerator[ClusterService, None]:
    yield ClusterService(db=context.db)
