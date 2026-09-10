from collections.abc import AsyncGenerator

from app.core.context import RequestContext
from app.core.exceptions import Invalid

from .services import ServingService


async def serving_svc_generator(context: RequestContext) -> AsyncGenerator[ServingService, None]:
    """Binds the service to the tenant the caller *is*, and to no other.

    The scope comes off the principal and there is no parameter that could name
    a different one. That is what makes `/v1/serving` safe to expose to a tenant
    credential: not a check that the tenant asked for themselves, but the
    absence of any way to ask for somebody else.

    An admin has no tenant of their own, so this surface has nothing to show
    them. They read a customer's view under `/v1/tenants/{ref}/serving`, where
    naming a tenant is the point of the path rather than a parameter smuggled
    onto a tenant-scoped one.
    """
    if context.principal.kind != "tenant":
        raise Invalid(
            "this surface is scoped to the calling tenant — read a customer's view at /v1/tenants/{ref}/serving"
        )
    if not context.principal.scope:
        raise Invalid("this session names no tenant")
    yield ServingService(db=context.db, tenant_uid=context.principal.scope)


async def tenant_serving_svc_generator(context: RequestContext, tenant: str) -> AsyncGenerator[ServingService, None]:
    """The same three reads, scoped to the customer named in the path.

    It needs no principal check of its own. `/v1/tenants` is outside
    `TENANT_SURFACE`, so a tenant credential is refused by
    `app.core.context.request_context` before any handler here is reached — the
    question "may this caller name that tenant" is answered by the routing table
    rather than by a condition somebody has to remember to write.
    """
    yield ServingService(db=context.db, tenant_uid=tenant)
