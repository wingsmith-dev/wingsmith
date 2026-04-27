from collections.abc import AsyncGenerator

from app.core.context import RequestContext
from app.integrations.huggingface import HuggingFaceClient

from .services import ArtifactService
from .services import ArtifactBaselineService


async def artifact_baseline_svc_generator(context: RequestContext) -> AsyncGenerator[ArtifactBaselineService, None]:
    """The recorder of a baseline is the principal, resolved here and nowhere else."""
    yield ArtifactBaselineService(db=context.db, actor=context.principal.name)


async def artifact_svc_generator(context: RequestContext) -> AsyncGenerator[ArtifactService, None]:
    """Builds the Hub client at the seam, so the service never constructs its own transport.

    One place to swap for a fake, and one place a token or a base url would be
    read from settings if either ever becomes per-request.
    """
    yield ArtifactService(db=context.db, hub=HuggingFaceClient())
