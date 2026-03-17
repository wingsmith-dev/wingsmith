from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import TopologyDomainService


async def topology_domain_svc_generator(context: RequestContext) -> AsyncGenerator[TopologyDomainService, None]:
    yield TopologyDomainService(db=context.db)
