from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import patch
from litestar import delete
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.filters import parse_labels
from app.core.schemas import ListResponse

from .depends import recipe_svc_generator
from .schemas import RecipeSchema
from .schemas import RecipeCreateRequest
from .schemas import RecipeUpdateRequest
from .services import RecipeService


class RecipeRoutes(Controller):
    tags = ["Recipes"]
    path = "/recipes"
    dependencies = {"svc": Provide(recipe_svc_generator)}

    @get()
    async def list_recipes(
        self,
        context: RequestContext,
        svc: RecipeService,
        limit: int | None = None,
        cursor: str | None = None,
        artifact: str | None = None,
        engine: str | None = None,
        device_class: str | None = Parameter(query="class", default=None),
        search: str | None = None,
        label: list[str] | None = None,
    ) -> Response[ListResponse[RecipeSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            artifact=artifact,
            engine=engine,
            device_class=device_class,
            search=search,
            labels=parse_labels(label),
        )
        return Response(result)

    @post()
    async def create_recipe(
        self, context: RequestContext, svc: RecipeService, data: RecipeCreateRequest
    ) -> Response[RecipeSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_recipe(self, context: RequestContext, svc: RecipeService, ref: str) -> Response[RecipeSchema]:
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_recipe(
        self,
        context: RequestContext,
        svc: RecipeService,
        ref: str,
        data: RecipeUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[RecipeSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_recipe(self, context: RequestContext, svc: RecipeService, ref: str) -> None:
        await svc.delete(ref)
