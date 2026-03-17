from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import PolicyService


async def policy_svc_generator(context: RequestContext) -> AsyncGenerator[PolicyService, None]:
    yield PolicyService(db=context.db)
