from litestar import Request
from litestar import Response
from litestar import Controller
from litestar import get
from litestar import put
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.exceptions import Invalid

from .schemas import Scale
from .schemas import ArtifactManifest
from .schemas import EndpointManifest
from .schemas import WorkloadManifest
from .services import ManifestService


async def manifest_svc_generator(context: RequestContext):
    yield ManifestService(db=context.db, actor=context.principal.name)


RESULT_HEADER = "X-Wingsmith-Result"


def _dry_run(value: str | None) -> bool:
    if value is None:
        return False
    if value != "All":
        raise Invalid(f"dryRun must be 'All', not {value!r}", {"field": "dryRun"})
    return True


def _applied(m, result: str) -> Response:
    return Response(m, status_code=201 if result == "created" else 200, headers={RESULT_HEADER: result})


class ManifestRoutes(Controller):
    """Objects as `plane.wingsmith.dev/v1alpha1` manifests (ADR 0022), applied with PUT (ADR 0029).

    Addressed by namespace and name, never by id: `{project}` is the project's slug (or id) and
    `{name}` the object's name, an Artifact's handle. A public Artifact has no namespace and is
    read at `/v1/manifests/artifacts/{name}`; a private one only under its own project.
    """

    tags = ["Manifests"]
    path = "/manifests"
    dependencies = {"svc": Provide(manifest_svc_generator)}

    @get("/workloads/{project:str}/{name:str}")
    async def get_workload_manifest(
        self, context: RequestContext, svc: ManifestService, project: str, name: str
    ) -> Response[WorkloadManifest]:
        return Response(await svc.workload(project, name))

    @get("/workloads/{project:str}/{name:str}/scale")
    async def get_workload_scale(
        self, context: RequestContext, svc: ManifestService, project: str, name: str
    ) -> Response[Scale]:
        return Response(await svc.scale(project, name))

    @put("/workloads/{project:str}/{name:str}/scale")
    async def put_workload_scale(
        self,
        context: RequestContext,
        svc: ManifestService,
        project: str,
        name: str,
        data: Scale,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[Scale]:
        """Sets the replica count. A shortfall is accepted and shown as `Unschedulable`;
        a stale `If-Match` is a 409 (ADR 0025)."""
        return Response(await svc.put_scale(project, name, data, if_match))

    @put("/workloads/{project:str}/{name:str}", status_code=200)
    async def put_workload_manifest(
        self,
        request: Request,
        svc: ManifestService,
        project: str,
        name: str,
        data: WorkloadManifest,
        if_match: str | None = Parameter(header="If-Match", default=None),
        dry_run: str | None = Parameter(query="dryRun", default=None),
    ) -> Response[WorkloadManifest]:
        """Creates (201) or replaces (200) the workload the manifest declares (ADR 0029, 0031).

        Three-way against the last-applied record: a field the last apply declared and this one
        leaves out is reset. A replay changes nothing. `X-Wingsmith-Result` is `created`,
        `configured` or `unchanged`; `?dryRun=All` returns the would-be object and keeps nothing. `If-Match` or
        `metadata.resourceVersion` makes it conditional (409 when stale); neither is
        last-write-wins. A changed immutable field is a 422 naming it.
        """
        dry = _dry_run(dry_run)
        m, result = await svc.put_workload(project, name, await request.json(), if_match, dry)
        return _applied(m, result)

    @get("/endpoints/{project:str}/{name:str}")
    async def get_endpoint_manifest(
        self, context: RequestContext, svc: ManifestService, project: str, name: str
    ) -> Response[EndpointManifest]:
        return Response(await svc.endpoint(project, name))

    @put("/endpoints/{project:str}/{name:str}", status_code=200)
    async def put_endpoint_manifest(
        self,
        request: Request,
        svc: ManifestService,
        project: str,
        name: str,
        data: EndpointManifest,
        if_match: str | None = Parameter(header="If-Match", default=None),
        dry_run: str | None = Parameter(query="dryRun", default=None),
    ) -> Response[EndpointManifest]:
        """Creates (201) or replaces (200) the endpoint the manifest declares (ADR 0029)."""
        dry = _dry_run(dry_run)
        m, result = await svc.put_endpoint(project, name, await request.json(), if_match, dry)
        return _applied(m, result)

    @get("/artifacts/{project:str}/{name:str}")
    async def get_artifact_manifest(
        self, context: RequestContext, svc: ManifestService, project: str, name: str
    ) -> Response[ArtifactManifest]:
        """A private artifact, under the project it is private to."""
        return Response(await svc.artifact(project, name))

    @get("/artifacts/{name:str}")
    async def get_public_artifact_manifest(
        self, context: RequestContext, svc: ManifestService, name: str
    ) -> Response[ArtifactManifest]:
        """An artifact in the public catalogue."""
        return Response(await svc.artifact(None, name))
