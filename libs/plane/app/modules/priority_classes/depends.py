from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import PriorityClassService


async def priority_class_svc_generator(context: RequestContext) -> AsyncGenerator[PriorityClassService, None]:
    yield PriorityClassService(db=context.db)
