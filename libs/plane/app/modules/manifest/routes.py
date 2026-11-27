from litestar import Response
from litestar import Controller
from litestar import get
from litestar import put
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext

from .schemas import Scale
from .schemas import ArtifactManifest
from .schemas import EndpointManifest
from .schemas import WorkloadManifest
from .services import ManifestService


async def manifest_svc_generator(context: RequestContext):
    yield ManifestService(db=context.db, actor=context.principal.name)


class ManifestRoutes(Controller):
    """Objects as `plane.wingsmith.dev/v1alpha1` manifests, read-only (ADR 0022).

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
        a stale `If-Match` is a 409 (ADR 0024)."""
        return Response(await svc.put_scale(project, name, data, if_match))

    @get("/endpoints/{project:str}/{name:str}")
    async def get_endpoint_manifest(
        self, context: RequestContext, svc: ManifestService, project: str, name: str
    ) -> Response[EndpointManifest]:
        return Response(await svc.endpoint(project, name))

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
