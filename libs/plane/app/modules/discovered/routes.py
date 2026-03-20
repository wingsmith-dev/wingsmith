from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import discovered_svc_generator
from .schemas import DecisionRequest
from .schemas import DiscoveredWorkloadSchema
from .services import IGNORED
from .services import TRACKED
from .services import DiscoveredWorkloadService


class DiscoveredWorkloadRoutes(Controller):
    """What the clusters were already running when we got there.

    `track` and `ignore` are POSTs rather than a PATCH on the workload because
    the object they write is not the workload: the sighting is observed and has
    exactly one writer, and these record a decision beside it. Neither verb sends
    anything to a cluster.
    """

    tags = ["Discovered"]
    path = "/discovered"
    dependencies = {"svc": Provide(discovered_svc_generator)}

    @get()
    async def list_discovered(
        self,
        context: RequestContext,
        svc: DiscoveredWorkloadService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> Response[ListResponse[DiscoveredWorkloadSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            namespace=namespace,
            status=status,
            search=search,
        )
        return Response(result)

    @get("/{ref:str}")
    async def get_discovered(
        self,
        context: RequestContext,
        svc: DiscoveredWorkloadService,
        ref: str,
        cluster: str | None = None,
    ) -> Response[DiscoveredWorkloadSchema]:
        return Response(await svc.get(ref, cluster=cluster))

    @post("/{ref:str}/track")
    async def track_discovered(
        self, context: RequestContext, svc: DiscoveredWorkloadService, ref: str, data: DecisionRequest
    ) -> Response[DiscoveredWorkloadSchema]:
        return Response(await svc.decide(ref, TRACKED, data))

    @post("/{ref:str}/ignore")
    async def ignore_discovered(
        self, context: RequestContext, svc: DiscoveredWorkloadService, ref: str, data: DecisionRequest
    ) -> Response[DiscoveredWorkloadSchema]:
        return Response(await svc.decide(ref, IGNORED, data))
