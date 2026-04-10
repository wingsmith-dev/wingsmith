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

from .depends import deployment_svc_generator
from .schemas import DeploymentSchema
from .schemas import DeploymentCreateRequest
from .schemas import DeploymentUpdateRequest
from .schemas import DeploymentAuthorityRequest
from .services import DeploymentService


class DeploymentRoutes(Controller):
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
        recipe: str | None = None,
        project: str | None = None,
        phase: str | None = None,
        workload_type: str | None = Parameter(query="type", default=None),
        label: list[str] | None = None,
    ) -> Response[ListResponse[DeploymentSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            recipe=recipe,
            project=project,
            phase=phase,
            type=workload_type,
            labels=parse_labels(label),
        )
        return Response(result)

    @post()
    async def create_deployment(
        self, context: RequestContext, svc: DeploymentService, data: DeploymentCreateRequest
    ) -> Response[DeploymentSchema]:
        return Response(await svc.create(data), status_code=201)

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

    @put("/{ref:str}/authority")
    async def set_deployment_authority(
        self,
        context: RequestContext,
        svc: DeploymentService,
        ref: str,
        data: DeploymentAuthorityRequest,
    ) -> Response[DeploymentSchema]:
        return Response(await svc.set_authority(ref, data))

    @delete("/{ref:str}", status_code=204)
    async def delete_deployment(self, context: RequestContext, svc: DeploymentService, ref: str) -> None:
        await svc.delete(ref)
