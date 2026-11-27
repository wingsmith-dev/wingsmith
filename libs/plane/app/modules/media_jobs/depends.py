from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import MediaJobService


async def media_job_svc_generator(context: RequestContext) -> AsyncGenerator[MediaJobService, None]:
    yield MediaJobService(db=context.db)
