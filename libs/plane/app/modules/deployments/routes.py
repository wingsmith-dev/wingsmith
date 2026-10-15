from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import patch
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import deployment_svc_generator
from .schemas import DeploymentSchema
from .schemas import DeploymentUpdateRequest
from .schemas import DeploymentAuthorityRequest
from .services import DeploymentService


class DeploymentRoutes(Controller):
    """Read-mostly: a deployment is created by placement, never by a caller.

    There is no `POST` and no `DELETE` here. One cluster's copy appears when
    placement expands a workload onto it and disappears when the workload
    contracts — through `workload.drain` or a scale change — so a caller who wants
    either asks the workload, which is the object they declared.
    """

    tags = ["Deployments"]
    path = "/deployments"
    dependencies = {"svc": Provide(deployment_svc_generator)}

    @get()
    async def list_deployments(
        self,
        context: RequestContext,
        svc: DeploymentService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        workload: str | None = None,
        project: str | None = None,
        phase: str | None = None,
    ) -> Response[ListResponse[DeploymentSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            workload=workload,
            project=project,
            phase=phase,
        )
        return Response(result)

    @get("/{ref:str}")
    async def get_deployment(
        self,
        context: RequestContext,
        svc: DeploymentService,
        ref: str,
        cluster: str | None = None,
    ) -> Response[DeploymentSchema]:
        return Response(await svc.get(ref, cluster=cluster))

    @patch("/{ref:str}")
    async def update_deployment(
        self,
        context: RequestContext,
        svc: DeploymentService,
        ref: str,
        data: DeploymentUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[DeploymentSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @post("/{ref:str}/authority", status_code=200)
    async def set_deployment_authority(
        self,
        context: RequestContext,
        svc: DeploymentService,
        ref: str,
        data: DeploymentAuthorityRequest,
    ) -> Response[DeploymentSchema]:
        return Response(await svc.set_authority(ref, data))
