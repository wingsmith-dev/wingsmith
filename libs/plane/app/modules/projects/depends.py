from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import ProjectService


async def project_svc_generator(context: RequestContext) -> AsyncGenerator[ProjectService, None]:
    yield ProjectService(db=context.db)
