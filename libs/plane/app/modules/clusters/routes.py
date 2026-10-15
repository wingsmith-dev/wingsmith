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
from app.core.exceptions import Forbidden

from .depends import cluster_svc_generator
from .schemas import ClusterSchema
from .schemas import TunnelAuthRequest
from .schemas import ClusterSetupSchema
from .schemas import ClusterRollupSchema
from .schemas import ClusterCreateRequest
from .schemas import ClusterUpdateRequest
from .schemas import TunnelIdentitySchema
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
        self,
        context: RequestContext,
        svc: ClusterService,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Response[ListResponse[ClusterRollupSchema]]:
        return Response(await svc.rollups(limit=limit, cursor=cursor))

    @get("/{ref:str}/rollup")
    async def get_rollup(self, context: RequestContext, svc: ClusterService, ref: str) -> Response[ClusterRollupSchema]:
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
        self, context: RequestContext, svc: ClusterService, ref: str
    ) -> Response[ClusterSetupSchema]:
        return Response(await svc.setup_instructions(ref))

    @post("/{ref:str}/enrolment-token")
    async def issue_enrolment_token(
        self, context: RequestContext, svc: ClusterService, ref: str
    ) -> Response[ClusterSetupSchema]:
        """Mints or rotates the cluster's enrolment token; the plaintext is in
        the response once and never again."""
        return Response(await svc.issue_enrolment_token(ref))

    @post("/tunnel-auth", status_code=200)
    async def verify_tunnel_token(
        self, context: RequestContext, svc: ClusterService, data: TunnelAuthRequest
    ) -> Response[TunnelIdentitySchema]:
        """Turns a cluster's enrolment token into that cluster's identity.

        `200`, not the `201` a POST defaults to: nothing is created, and the
        gateway treats any status it was not promised as "the plane could not be
        reached" — which refuses every dial while looking like a network fault.
        It is a POST only because the credential being checked must not travel in
        a query string.

        The gateway principal only. The gateway terminates the outbound tunnels
        serving agents dial and needs to know which cluster is on the other end
        of one. An admin session is refused rather than accommodated — a human
        credential that can turn tokens into identities is a credential that can
        sweep for them, and no console screen asks this question.
        """
        if context.principal.kind != "gateway":
            raise Forbidden("agent verification is for the gateway's service credential only")
        return Response(await svc.verify_tunnel_token(data.token))

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
    async def delete_cluster(
        self,
        context: RequestContext,
        svc: ClusterService,
        ref: str,
        if_match: str | None = Parameter(header="If-Match", default=None),
        force: bool = False,
    ) -> None:
        """Needs `If-Match` or `force=true`, and refuses while anything is bound to the cluster."""
        await svc.delete(ref, if_match=if_match, force=force)

    @put("/{ref:str}/serving")
    async def set_serving(
        self, context: RequestContext, svc: ClusterService, ref: str, data: ClusterServingRequest
    ) -> Response[ClusterSchema]:
        return Response(await svc.set_serving(ref, data))

    @delete("/{ref:str}/serving", status_code=204)
    async def clear_serving(self, context: RequestContext, svc: ClusterService, ref: str) -> None:
        await svc.clear_serving(ref)
