from collections.abc import AsyncGenerator

from app.core.context import RequestContext

from .services import RecipeService


async def recipe_svc_generator(context: RequestContext) -> AsyncGenerator[RecipeService, None]:
    yield RecipeService(db=context.db)
