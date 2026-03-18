"""Allocations: observed, read-only.

`GET` list and `GET` one, and nothing else. An allocation is decided by placement
and reported by the cluster, so `POST /v1/allocations` is a category error rather
than a missing feature — capacity is released when a workload ends, not when a
client deletes a row.

`?device=dev_...` answers "which allocation holds this device right now" straight
off the partial unique index that enforces one live holder per device.

Ids are prefixed `alc_` uids, like every other resource, since the schema
migration gave `Allocation` its own `uid` column.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import allocation_svc_generator
from .schemas import AllocationSchema
from .services import AllocationService


class AllocationRoutes(Controller):
    tags = ["Allocations"]
    path = "/allocations"
    dependencies = {"svc": Provide(allocation_svc_generator)}

    @get()
    async def list_allocations(
        self,
        context: RequestContext,
        svc: AllocationService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        project: str | None = None,
        device_class: str | None = Parameter(query="class", default=None, required=False),
        phase: str | None = None,
        over_quota: bool | None = None,
        live: bool | None = None,
        replica: str | None = None,
        device: str | None = None,
    ) -> Response[ListResponse[AllocationSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            project=project,
            device_class=device_class,
            phase=phase,
            over_quota=over_quota,
            live=live,
            replica=replica,
            device=device,
        )
        return Response(result)

    @get("/{ref:str}")
    async def get_allocation(
        self, context: RequestContext, svc: AllocationService, ref: str
    ) -> Response[AllocationSchema]:
        return Response(await svc.get(ref))
