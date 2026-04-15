from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import WorkloadService


async def workload_svc_generator(context: RequestContext) -> AsyncGenerator[WorkloadService, None]:
    yield WorkloadService(db=context.db)
