"""Nodes: observed, read-only.

`GET` list and `GET` one, and nothing else. There is no `POST`, `PATCH` or
`DELETE` because a node is discovered, not declared, and the operations
that act on one (drain, cordon) are `POST /v1/operations` with a type
and a target, not verbs hung off this path.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.filters import parse_labels
from app.core.schemas import ListResponse

from .depends import node_svc_generator
from .schemas import NodeSchema
from .schemas import NodesSummarySchema
from .services import NodeService


class NodeRoutes(Controller):
    tags = ["Nodes"]
    path = "/nodes"
    dependencies = {"svc": Provide(node_svc_generator)}

    @get()
    async def list_nodes(
        self,
        context: RequestContext,
        svc: NodeService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        pool: str | None = None,
        condition: str | None = None,
        ready: bool | None = None,
        cordoned: bool | None = None,
        search: str | None = None,
        label: list[str] | None = None,
    ) -> Response[ListResponse[NodeSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            pool=pool,
            condition=condition,
            ready=ready,
            cordoned=cordoned,
            search=search,
            labels=parse_labels(label),
        )
        return Response(result)

    @get("/summary")
    async def nodes_summary(self, context: RequestContext, svc: NodeService) -> Response[NodesSummarySchema]:
        return Response(await svc.summary())

    @get("/{ref:str}")
    async def get_node(
        self, context: RequestContext, svc: NodeService, ref: str, cluster: str | None = None
    ) -> Response[NodeSchema]:
        return Response(await svc.get(ref, cluster))
