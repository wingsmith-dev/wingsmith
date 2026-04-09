from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import BenchmarkService


async def benchmark_svc_generator(context: RequestContext) -> AsyncGenerator[BenchmarkService, None]:
    """A run records who asked for it, so the actor is a constructor argument."""
    yield BenchmarkService(db=context.db, actor=context.principal.name)
