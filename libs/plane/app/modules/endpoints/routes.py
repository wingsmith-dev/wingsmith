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
from .schemas import ResolvedTableSchema
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
        workload: str | None = None,
        search: str | None = None,
        label: list[str] | None = None,
    ) -> Response[ListResponse[EndpointSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            project=project,
            artifact=artifact,
            workload=workload,
            search=search,
            labels=parse_labels(label),
        )
        return Response(result)

    @post()
    async def create_endpoint(
        self, context: RequestContext, svc: EndpointService, data: EndpointCreateRequest
    ) -> Response[EndpointSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/resolved")
    async def resolve_all(
        self,
        context: RequestContext,
        svc: EndpointService,
        if_none_match: str | None = Parameter(header="If-None-Match", default=None),
    ) -> Response[ResolvedTableSchema | None]:
        """The whole routing table, for something that holds it in memory.

        Unpaginated on purpose: a caller that assembled half a routing table
        would route half its traffic into a hole, and the table is one row per
        endpoint. `If-None-Match` makes the steady state a `304`, which is what
        polling this every few seconds should cost.
        """
        table = await svc.resolved()
        if if_none_match is not None and if_none_match == table.revision:
            return Response(None, status_code=304, headers={"ETag": table.revision})
        return Response(table, headers={"ETag": table.revision})

    @get("/{ref:str}/resolved")
    async def resolve_one(
        self, context: RequestContext, svc: EndpointService, ref: str
    ) -> Response[ResolvedTableSchema]:
        return Response(await svc.resolved(ref))

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
