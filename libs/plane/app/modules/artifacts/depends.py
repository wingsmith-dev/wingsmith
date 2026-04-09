from collections.abc import AsyncGenerator

from app.core.context import RequestContext
from app.integrations.huggingface import HuggingFaceClient

from .services import ArtifactService


async def artifact_svc_generator(context: RequestContext) -> AsyncGenerator[ArtifactService, None]:
    """Builds the Hub client at the seam, so the service never constructs its own transport.

    One place to swap for a fake, and one place a token or a base url would be
    read from settings if either ever becomes per-request.
    """
    yield ArtifactService(db=context.db, hub=HuggingFaceClient())
