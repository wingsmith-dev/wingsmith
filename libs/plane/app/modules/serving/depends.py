from collections.abc import AsyncGenerator

from litestar import Request

from app.core.context import RequestContext
from app.core.exceptions import Invalid

from .services import ServingService


async def serving_svc_generator(context: RequestContext, request: Request) -> AsyncGenerator[ServingService, None]:
    """Binds the service to exactly one tenant before any handler runs.

    A tenant principal is bound to its own scope and cannot name another: the
    `tenant` parameter is read only for an admin, for whom it is the only way to
    look at what a customer sees. Deciding this here rather than in each handler
    means a route added to this module later is scoped whether or not its author
    thought about it.
    """
    if context.principal.kind == "tenant":
        if not context.principal.scope:
            raise Invalid("this session names no tenant")
        yield ServingService(db=context.db, tenant_uid=context.principal.scope)
        return

    ref = request.query_params.get("tenant")
    if not ref:
        raise Invalid("tenant is required")
    yield ServingService(db=context.db, tenant_uid=ref)
