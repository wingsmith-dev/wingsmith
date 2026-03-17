from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import DeploymentService


async def deployment_svc_generator(context: RequestContext) -> AsyncGenerator[DeploymentService, None]:
    yield DeploymentService(db=context.db)
