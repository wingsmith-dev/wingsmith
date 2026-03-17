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

from .depends import endpoint_svc_generator
from .schemas import EndpointSchema
from .schemas import EndpointCreateRequest
from .schemas import EndpointUpdateRequest
from .services import EndpointService


class EndpointRoutes(Controller):
    """The global name layer. No handler here takes a cluster."""

    tags = ["Endpoints"]
    path = "/endpoints"
    dependencies = {"svc": Provide(endpoint_svc_generator)}

    @get()
    async def list_endpoints(
        self,
        context: RequestContext,
        svc: EndpointService,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        artifact: str | None = None,
        search: str | None = None,
        label: list[str] | None = None,
    ) -> Response[ListResponse[EndpointSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            project=project,
            artifact=artifact,
            search=search,
            labels=parse_labels(label),
        )
        return Response(result)

    @post()
    async def create_endpoint(
        self, context: RequestContext, svc: EndpointService, data: EndpointCreateRequest
    ) -> Response[EndpointSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_endpoint(self, context: RequestContext, svc: EndpointService, ref: str) -> Response[EndpointSchema]:
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_endpoint(
        self,
        context: RequestContext,
        svc: EndpointService,
        ref: str,
        data: EndpointUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[EndpointSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_endpoint(self, context: RequestContext, svc: EndpointService, ref: str) -> None:
        await svc.delete(ref)
