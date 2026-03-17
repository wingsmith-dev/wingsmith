from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import OperationService


async def operation_svc_generator(context: RequestContext) -> AsyncGenerator[OperationService, None]:
    """The caller identity is a constructor argument, not an afterthought.

    An operation records who asked for it, so the actor comes from the resolved
    principal on the way in. Auth is stubbed today and every request resolves to
    the same one, which changes the value here and nothing else.
    """
    yield OperationService(db=context.db, actor=context.principal.name)
