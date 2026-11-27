from litestar import Request
from litestar import Response
from litestar import Controller
from litestar import get
from litestar import put
from litestar import delete
from litestar.di import Provide
from litestar.params import Parameter
from litestar.response import Stream

from app.db.engine import session_factory
from app.core.context import RequestContext
from app.core.ownership import field_manager
from app.core.exceptions import Invalid
from app.core.exceptions import InvalidParameter

from . import watch as watching
from .schemas import Scale
from .schemas import ArtifactManifest
from .schemas import EndpointManifest
from .schemas import WorkloadManifest
from .schemas import ArtifactManifestList
from .schemas import EndpointManifestList
from .schemas import WorkloadManifestList
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


async def _list_or_watch(
    collection: str,
    project: str | None,
    watch: bool,
    resource_version: str | None,
    label_selector: str | None,
    field_selector: str | None,
    timeout_seconds: int | None,
) -> Response:
    selector = watching.parse_selector(label_selector, field_selector, None)
    sessions = session_factory()
    if not watch:
        return Response(await watching.list_manifests(sessions, collection, project, selector))
    rv = None
    if resource_version not in (None, "", "0"):
        if not resource_version.isdigit():
            raise InvalidParameter("resourceVersion must be a number", {"field": "resourceVersion"})
        rv = int(resource_version)
    w = watching.Watch(sessions, collection, project, selector, rv, watching.timeout_of(timeout_seconds))
    await w.check()
    watching.fanout().admit()
    return Stream(await watching.stream(w), media_type="application/json")


LIST_DOC = """Lists every {kind} (`{list}`), current as of `metadata.resourceVersion`, or with
`?watch=true` streams changes as JSON lines (`{{"type": "ADDED|MODIFIED|DELETED|BOOKMARK|ERROR",
"object": ...}}`, ADR 0039). A watch resumes after `resourceVersion` (unset: an `ADDED` per current
object first); an RV the change log has compacted is a 410, or an `ERROR` event with a 410 `Status`
mid-stream: relist and watch again. `labelSelector` takes `k=v` and `k!=v` terms, `fieldSelector`
`metadata.name=` and `metadata.namespace=`. `timeoutSeconds` ends the stream (default about 300,
at most 1800); `BOOKMARK`s carry the current RV every 30s. A full plane answers 429."""


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

    @get("/workloads", description=LIST_DOC.format(kind="Workload", list="WorkloadList"))
    async def list_workload_manifests(
        self,
        watch: bool = False,
        resource_version: str | None = Parameter(query="resourceVersion", default=None),
        label_selector: str | None = Parameter(query="labelSelector", default=None),
        field_selector: str | None = Parameter(query="fieldSelector", default=None),
        timeout_seconds: int | None = Parameter(query="timeoutSeconds", default=None, ge=1),
        allow_watch_bookmarks: bool = Parameter(query="allowWatchBookmarks", default=True),
    ) -> Response[WorkloadManifestList]:
        return await _list_or_watch(
            "workloads", None, watch, resource_version, label_selector, field_selector, timeout_seconds
        )

    @get("/workloads/{project:str}", description=LIST_DOC.format(kind="Workload", list="WorkloadList"))
    async def list_project_workload_manifests(
        self,
        project: str,
        watch: bool = False,
        resource_version: str | None = Parameter(query="resourceVersion", default=None),
        label_selector: str | None = Parameter(query="labelSelector", default=None),
        field_selector: str | None = Parameter(query="fieldSelector", default=None),
        timeout_seconds: int | None = Parameter(query="timeoutSeconds", default=None, ge=1),
        allow_watch_bookmarks: bool = Parameter(query="allowWatchBookmarks", default=True),
    ) -> Response[WorkloadManifestList]:
        return await _list_or_watch(
            "workloads", project, watch, resource_version, label_selector, field_selector, timeout_seconds
        )

    @get("/endpoints", description=LIST_DOC.format(kind="Endpoint", list="EndpointList"))
    async def list_endpoint_manifests(
        self,
        watch: bool = False,
        resource_version: str | None = Parameter(query="resourceVersion", default=None),
        label_selector: str | None = Parameter(query="labelSelector", default=None),
        field_selector: str | None = Parameter(query="fieldSelector", default=None),
        timeout_seconds: int | None = Parameter(query="timeoutSeconds", default=None, ge=1),
        allow_watch_bookmarks: bool = Parameter(query="allowWatchBookmarks", default=True),
    ) -> Response[EndpointManifestList]:
        return await _list_or_watch(
            "endpoints", None, watch, resource_version, label_selector, field_selector, timeout_seconds
        )

    @get("/endpoints/{project:str}", description=LIST_DOC.format(kind="Endpoint", list="EndpointList"))
    async def list_project_endpoint_manifests(
        self,
        project: str,
        watch: bool = False,
        resource_version: str | None = Parameter(query="resourceVersion", default=None),
        label_selector: str | None = Parameter(query="labelSelector", default=None),
        field_selector: str | None = Parameter(query="fieldSelector", default=None),
        timeout_seconds: int | None = Parameter(query="timeoutSeconds", default=None, ge=1),
        allow_watch_bookmarks: bool = Parameter(query="allowWatchBookmarks", default=True),
    ) -> Response[EndpointManifestList]:
        return await _list_or_watch(
            "endpoints", project, watch, resource_version, label_selector, field_selector, timeout_seconds
        )

    @get("/artifacts", description=LIST_DOC.format(kind="Artifact", list="ArtifactList"))
    async def list_artifact_manifests(
        self,
        watch: bool = False,
        resource_version: str | None = Parameter(query="resourceVersion", default=None),
        label_selector: str | None = Parameter(query="labelSelector", default=None),
        field_selector: str | None = Parameter(query="fieldSelector", default=None),
        timeout_seconds: int | None = Parameter(query="timeoutSeconds", default=None, ge=1),
        allow_watch_bookmarks: bool = Parameter(query="allowWatchBookmarks", default=True),
    ) -> Response[ArtifactManifestList]:
        """Artifacts in every scope; narrow with `fieldSelector=metadata.namespace=<project>`
        (`/artifacts/{name}` is the public artifact read)."""
        return await _list_or_watch(
            "artifacts", None, watch, resource_version, label_selector, field_selector, timeout_seconds
        )

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
        manager: str | None = Parameter(query="fieldManager", default=None),
        force: bool = False,
    ) -> Response[Scale]:
        """Sets the replica count. A shortfall is accepted and shown as `Unschedulable`;
        a stale `If-Match` is a 409 (ADR 0025)."""
        return Response(
            await svc.put_scale(
                project, name, data, if_match, force=force, manager=None if manager is None else field_manager(manager)
            )
        )

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
        manager: str | None = Parameter(query="fieldManager", default=None),
        force: bool = False,
    ) -> Response[WorkloadManifest]:
        """Creates (201) or replaces (200) the workload the manifest declares (ADR 0029, 0032).

        Three-way against the last-applied record: a field the last apply declared and this one
        leaves out is reset. A replay changes nothing. `X-Wingsmith-Result` is `created`,
        `configured` or `unchanged`; `?dryRun=All` returns the would-be object and keeps nothing. `If-Match` or
        `metadata.resourceVersion` makes it conditional (409 when stale); neither is
        last-write-wins. A changed immutable field is a 422 naming it.

        `?fieldManager=` names who applies (default `wings-cli`, ADR 0033). An object another
        manager applied is a 409 naming it unless `?force=true`, which takes it over. An observed
        workload is a 422: adopt it first.
        """
        dry = _dry_run(dry_run)
        m, result = await svc.put_workload(
            project, name, await request.json(), if_match, dry, manager=field_manager(manager), force=force
        )
        return _applied(m, result)

    @delete("/workloads/{project:str}/{name:str}", status_code=204)
    async def delete_workload_manifest(
        self,
        svc: ManifestService,
        project: str,
        name: str,
        manager: str | None = Parameter(query="fieldManager", default=None),
        force: bool = False,
    ) -> None:
        """Deletes the workload. One another field manager applied (another `--field-manager`, or a
        member cluster's `k8s:<cluster>/<namespace>`) is a 409 naming it unless `?force=true`; a
        cluster-declared one would only be applied again from its custom resource (ADR 0042)."""
        await svc.delete_manifest("workload", project, name, field_manager(manager), force=force)

    @delete("/endpoints/{project:str}/{name:str}", status_code=204)
    async def delete_endpoint_manifest(
        self,
        svc: ManifestService,
        project: str,
        name: str,
        manager: str | None = Parameter(query="fieldManager", default=None),
        force: bool = False,
    ) -> None:
        """Deletes the endpoint, with the field manager check of the workload delete."""
        await svc.delete_manifest("endpoint", project, name, field_manager(manager), force=force)

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
        manager: str | None = Parameter(query="fieldManager", default=None),
        force: bool = False,
    ) -> Response[EndpointManifest]:
        """Creates (201) or replaces (200) the endpoint the manifest declares (ADR 0029). Ownership
        and `?fieldManager=` / `?force=true` as for a workload (ADR 0033)."""
        dry = _dry_run(dry_run)
        m, result = await svc.put_endpoint(
            project, name, await request.json(), if_match, dry, manager=field_manager(manager), force=force
        )
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
