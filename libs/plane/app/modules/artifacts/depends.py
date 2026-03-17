from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import ArtifactService


async def artifact_svc_generator(context: RequestContext) -> AsyncGenerator[ArtifactService, None]:
    yield ArtifactService(db=context.db)
