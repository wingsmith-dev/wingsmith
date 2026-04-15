from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import delete
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.schemas import ListResponse
from app.modules.workloads.schemas import WorkloadSchema

from .derive import RecipeDraft
from .depends import discovered_svc_generator
from .schemas import PromoteRequest
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

    `promote` is the one that does, and it is the only write in this controller
    that produces declared state. It returns a deployment rather than a workload
    because that is what it made: from here the object is managed through
    `/v1/deployments` like any other, with the difference that the cluster will
    refuse to reconcile it if what is running stops matching what we adopted.
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

    @get("/{ref:str}/recipe-draft")
    async def draft_recipe(
        self,
        context: RequestContext,
        svc: DiscoveredWorkloadService,
        ref: str,
        cluster: str | None = None,
    ) -> Response[RecipeDraft]:
        return Response(await svc.recipe_draft(ref, cluster=cluster))

    @post("/{ref:str}/track")
    async def track_discovered(
        self, context: RequestContext, svc: DiscoveredWorkloadService, ref: str, data: DecisionRequest
    ) -> Response[DiscoveredWorkloadSchema]:
        return Response(await svc.decide(ref, TRACKED, data))

    @post("/{ref:str}/promote", status_code=201)
    async def promote_discovered(
        self, context: RequestContext, svc: DiscoveredWorkloadService, ref: str, data: PromoteRequest
    ) -> Response[WorkloadSchema]:
        return Response(await svc.promote(ref, data), status_code=201)

    @delete("/{ref:str}", status_code=204)
    async def dismiss_discovered(
        self, context: RequestContext, svc: DiscoveredWorkloadService, ref: str, data: DecisionRequest | None = None
    ) -> None:
        """Takes a workload off the list for good.

        A DELETE even though nothing observed is deleted, because what a caller is
        removing is the entry in front of them and the sighting underneath it is
        not theirs to remove — ingestion owns it and would write it back within an
        observation interval anyway. The decision is recorded, the list honours it,
        and `POST /restore` undoes it. Nothing here reaches a cluster: the workload
        goes on running exactly as it was.
        """
        await svc.dismiss(ref, data or DecisionRequest())

    @post("/{ref:str}/restore")
    async def restore_discovered(
        self, context: RequestContext, svc: DiscoveredWorkloadService, ref: str
    ) -> Response[DiscoveredWorkloadSchema]:
        """Un-dismisses a workload, putting it back on the list."""
        return Response(await svc.restore(ref))

    @post("/{ref:str}/ignore")
    async def ignore_discovered(
        self, context: RequestContext, svc: DiscoveredWorkloadService, ref: str, data: DecisionRequest
    ) -> Response[DiscoveredWorkloadSchema]:
        return Response(await svc.decide(ref, IGNORED, data))
