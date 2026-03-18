"""Priority classes: what preempts what.

The route path is `/v1/priority-classes` per `contract/API.md` §6; the Python
package is underscored because a hyphen is not an identifier. Ids are `prc_`:
`pol_` was already spent on pools, so policy and priority class took `plc_` and
`prc_` rather than crowding a prefix that was taken.
"""

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

from .depends import priority_class_svc_generator
from .schemas import PriorityClassSchema
from .schemas import PriorityClassCreateRequest
from .schemas import PriorityClassUpdateRequest
from .services import PriorityClassService


class PriorityClassRoutes(Controller):
    tags = ["Priority Classes"]
    path = "/priority-classes"
    dependencies = {"svc": Provide(priority_class_svc_generator)}

    @get()
    async def list_priority_classes(
        self,
        context: RequestContext,
        svc: PriorityClassService,
        limit: int | None = None,
        cursor: str | None = None,
        preemptible: bool | None = None,
        preempts: bool | None = None,
        search: str | None = None,
    ) -> Response[ListResponse[PriorityClassSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            preemptible=preemptible,
            preempts=preempts,
            search=search,
        )
        return Response(result)

    @post()
    async def create_priority_class(
        self, context: RequestContext, svc: PriorityClassService, data: PriorityClassCreateRequest
    ) -> Response[PriorityClassSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_priority_class(
        self, context: RequestContext, svc: PriorityClassService, ref: str
    ) -> Response[PriorityClassSchema]:
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_priority_class(
        self,
        context: RequestContext,
        svc: PriorityClassService,
        ref: str,
        data: PriorityClassUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[PriorityClassSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_priority_class(self, context: RequestContext, svc: PriorityClassService, ref: str) -> None:
        await svc.delete(ref)
