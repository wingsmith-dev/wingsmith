from litestar import Request
from litestar import Response
from litestar import Controller
from litestar import get
from litestar import put
from litestar import post
from litestar import patch
from litestar import delete
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.filters import parse_labels
from app.core.schemas import ListResponse

from .depends import cluster_svc_generator
from .schemas import ClusterSchema
from .schemas import ClusterSetupSchema
from .schemas import ClusterRollupSchema
from .schemas import ClusterCreateRequest
from .schemas import ClusterUpdateRequest
from .schemas import ClusterHeadroomSchema
from .schemas import ClusterServingRequest
from .services import ClusterService


class ClusterRoutes(Controller):
    tags = ["Clusters"]
    path = "/clusters"
    dependencies = {"svc": Provide(cluster_svc_generator)}

    @get()
    async def list_clusters(
        self,
        context: RequestContext,
        svc: ClusterService,
        limit: int | None = None,
        cursor: str | None = None,
        region: str | None = None,
        phase: str | None = None,
        search: str | None = None,
        label: list[str] | None = None,
    ) -> Response[ListResponse[ClusterSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            region=region,
            phase=phase,
            search=search,
            labels=parse_labels(label),
        )
        return Response(result)

    @get("/rollups")
    async def list_rollups(
        self, context: RequestContext, svc: ClusterService
    ) -> Response[ListResponse[ClusterRollupSchema]]:
        return Response(await svc.rollups())

    @get("/{ref:str}/rollup")
    async def get_rollup(
        self, context: RequestContext, svc: ClusterService, ref: str
    ) -> Response[ClusterRollupSchema]:
        return Response(await svc.rollup(ref))

    @get("/{ref:str}/headroom")
    async def get_headroom(
        self, context: RequestContext, svc: ClusterService, ref: str
    ) -> Response[ClusterHeadroomSchema]:
        return Response(await svc.headroom(ref))

    @post()
    async def create_cluster(
        self, context: RequestContext, svc: ClusterService, data: ClusterCreateRequest
    ) -> Response[ClusterSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_cluster(self, context: RequestContext, svc: ClusterService, ref: str) -> Response[ClusterSchema]:
        return Response(await svc.get(ref))

    @get("/{ref:str}/setup")
    async def cluster_setup(
        self, request: Request, context: RequestContext, svc: ClusterService, ref: str
    ) -> Response[ClusterSetupSchema]:
        host = request.headers.get("host", "")
        return Response(await svc.setup_instructions(ref, host))

    @post("/{ref:str}/enrolment-token")
    async def issue_enrolment_token(
        self, request: Request, context: RequestContext, svc: ClusterService, ref: str
    ) -> Response[ClusterSetupSchema]:
        """Mints or rotates the cluster's enrolment token; the plaintext is in
        the response once and never again."""
        host = request.headers.get("host", "")
        return Response(await svc.issue_enrolment_token(ref, host))

    @patch("/{ref:str}")
    async def update_cluster(
        self,
        context: RequestContext,
        svc: ClusterService,
        ref: str,
        data: ClusterUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[ClusterSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_cluster(self, context: RequestContext, svc: ClusterService, ref: str) -> None:
        await svc.delete(ref)

    @put("/{ref:str}/serving")
    async def set_serving(
        self, context: RequestContext, svc: ClusterService, ref: str, data: ClusterServingRequest
    ) -> Response[ClusterSchema]:
        return Response(await svc.set_serving(ref, data))

    @delete("/{ref:str}/serving", status_code=204)
    async def clear_serving(self, context: RequestContext, svc: ClusterService, ref: str) -> None:
        await svc.clear_serving(ref)
