from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import patch
from litestar import delete
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import component_svc_generator
from .schemas import ComponentSchema
from .schemas import ComponentCreateRequest
from .schemas import ComponentUpdateRequest
from .services import ComponentService


class ComponentRoutes(Controller):
    """The per-cluster software rollout surface.

    Writes land on the declared half only. The observed half comes back on every
    response but has no endpoint that writes it, because ingestion is its only
    writer.
    """

    tags = ["Components"]
    path = "/components"
    dependencies = {"svc": Provide(component_svc_generator)}

    @get()
    async def list_components(
        self,
        context: RequestContext,
        svc: ComponentService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        name: str | None = None,
        kind: str | None = None,
        behind: bool | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> Response[ListResponse[ComponentSchema]]:
        """`?name=agent&behind=true` lists the clusters behind on that component."""
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            name=name,
            kind=kind,
            behind=behind,
            enabled=enabled,
            search=search,
        )
        return Response(result)

    @post()
    async def create_component(
        self, context: RequestContext, svc: ComponentService, data: ComponentCreateRequest
    ) -> Response[ComponentSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_component(
        self, context: RequestContext, svc: ComponentService, ref: str, cluster: str | None = None
    ) -> Response[ComponentSchema]:
        """`cluster` disambiguates a name: one name is unique per cluster, not globally."""
        return Response(await svc.get(ref, cluster=cluster))

    @patch("/{ref:str}")
    async def update_component(
        self,
        context: RequestContext,
        svc: ComponentService,
        ref: str,
        data: ComponentUpdateRequest,
        cluster: str | None = None,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[ComponentSchema]:
        return Response(await svc.update(ref, data, if_match=if_match, cluster=cluster))

    @delete("/{ref:str}", status_code=204)
    async def delete_component(
        self, context: RequestContext, svc: ComponentService, ref: str, cluster: str | None = None
    ) -> None:
        await svc.delete(ref, cluster=cluster)
