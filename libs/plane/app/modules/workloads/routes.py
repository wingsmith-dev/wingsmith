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

from .depends import workload_svc_generator
from .schemas import WorkloadSchema
from .schemas import WorkloadCreateRequest
from .schemas import WorkloadUpdateRequest
from .schemas import WorkloadPlacementSchema
from .services import WorkloadService


class WorkloadRoutes(Controller):
    """The global object: what you asked to run, across every cluster it may use.

    `{ref}` is a `wkl_` id or a name. A name is unique only within its project, so
    a bare one is resolved inside the `project` filter and is ambiguous without it —
    which is a 400 naming the candidates rather than a guess.
    """

    tags = ["Workloads"]
    path = "/workloads"
    dependencies = {"svc": Provide(workload_svc_generator)}

    @get()
    async def list_workloads(
        self,
        context: RequestContext,
        svc: WorkloadService,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        kind: str | None = None,
        phase: str | None = None,
        artifact: str | None = None,
        serving_digest: str | None = None,
        device_class: str | None = Parameter(query="class", default=None),
        cluster: str | None = None,
        label: list[str] | None = None,
    ) -> Response[ListResponse[WorkloadSchema]]:
        """One page of workloads, narrowed by whatever the caller states.

        `serving_digest` is an exact match on the whole `svg_...` string: it answers
        "is anything already serving exactly this configuration", which is the
        question a create form asks before it mints a second workload for one
        engine. `artifact` narrows to the weights, so the two together are "which
        workloads serve these weights, and which of them are the same fit".

        `class` is the device class the workload asks for, spelled the way the
        device-class routes spell it, and answers "what is asking for H100s".
        """
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            project=project,
            kind=kind,
            phase=phase,
            artifact=artifact,
            serving_digest=serving_digest,
            device_class=device_class,
            cluster=cluster,
            labels=parse_labels(label),
        )
        return Response(result)

    @post()
    async def create_workload(
        self, context: RequestContext, svc: WorkloadService, data: WorkloadCreateRequest
    ) -> Response[WorkloadSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_workload(
        self, context: RequestContext, svc: WorkloadService, ref: str, project: str | None = None
    ) -> Response[WorkloadSchema]:
        return Response(await svc.get(ref, project))

    @get("/{ref:str}/placement")
    async def workload_placement(
        self, context: RequestContext, svc: WorkloadService, ref: str, project: str | None = None
    ) -> Response[WorkloadPlacementSchema]:
        """Where the workload physically landed, derived from its live allocations."""
        return Response(await svc.placement(ref, project))

    @patch("/{ref:str}")
    async def update_workload(
        self,
        context: RequestContext,
        svc: WorkloadService,
        ref: str,
        data: WorkloadUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[WorkloadSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_workload(self, context: RequestContext, svc: WorkloadService, ref: str) -> None:
        await svc.delete(ref)
