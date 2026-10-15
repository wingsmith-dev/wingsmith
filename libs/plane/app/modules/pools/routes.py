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

from .depends import pool_svc_generator
from .schemas import PoolSchema
from .schemas import PoolCreateRequest
from .schemas import PoolUpdateRequest
from .services import PoolService


class PoolRoutes(Controller):
    tags = ["Pools"]
    path = "/pools"
    dependencies = {"svc": Provide(pool_svc_generator)}

    @get()
    async def list_pools(
        self,
        context: RequestContext,
        svc: PoolService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        device_class: str | None = Parameter(query="class", default=None),
        enabled: bool | None = None,
        search: str | None = None,
        label: list[str] | None = None,
    ) -> Response[ListResponse[PoolSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            device_class=device_class,
            enabled=enabled,
            search=search,
            labels=parse_labels(label),
        )
        return Response(result)

    @post()
    async def create_pool(
        self, context: RequestContext, svc: PoolService, data: PoolCreateRequest
    ) -> Response[PoolSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_pool(
        self, context: RequestContext, svc: PoolService, ref: str, cluster: str | None = None
    ) -> Response[PoolSchema]:
        return Response(await svc.get(ref, cluster=cluster))

    @patch("/{ref:str}")
    async def update_pool(
        self,
        context: RequestContext,
        svc: PoolService,
        ref: str,
        data: PoolUpdateRequest,
        cluster: str | None = None,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[PoolSchema]:
        return Response(await svc.update(ref, data, if_match=if_match, cluster=cluster))

    @delete("/{ref:str}", status_code=204)
    async def delete_pool(
        self, context: RequestContext, svc: PoolService, ref: str, cluster: str | None = None
    ) -> None:
        """`cluster` narrows a pool name, which is unique only within one cluster."""
        await svc.delete(ref, cluster=cluster)
