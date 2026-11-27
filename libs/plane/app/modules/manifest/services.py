"""Reads one object as a manifest, through the same service read the HTTP API uses.

Each kind is fetched with its own service's `get`, so visibility is whatever that read enforces,
and then held to the manifest's identity: `name` is the object's name (an Artifact's handle), not
an id, and the object must live in exactly the namespace asked for. An artifact private to one
project is reachable only under that project; a public one only without a namespace.
"""

import datetime as dt

import msgspec

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import OPERATION
from app.core.ids import new_uid
from app.db.models import Policy
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Operation
from app.db.models import Deployment
from app.db.models import WorkloadRevision
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.modules.artifacts.scope import visible
from app.modules.artifacts.scope import visible_ids
from app.modules.workloads.views import _revision
from app.modules.endpoints.schemas import EndpointUpdateRequest
from app.modules.workloads.schemas import WorkloadServingUpdate
from app.modules.workloads.schemas import WorkloadUpdateRequest
from app.modules.workloads.schemas import WorkloadScaleParameters
from app.modules.artifacts.services import ArtifactService
from app.modules.endpoints.services import EndpointService
from app.modules.workloads.services import MANAGED
from app.modules.workloads.services import WorkloadService
from app.modules.workloads.services import constraint_name
from app.modules.workloads.repository import WorkloadRepository

from .schemas import NOTE_ANNOTATION
from .schemas import Scale
from .schemas import ArtifactManifest
from .schemas import EndpointManifest
from .schemas import WorkloadManifest
from .translate import SERVING_FIELDS
from .translate import DYNAMO_RUNTIME_FIELDS
from .translate import Refs
from .translate import Placement
from .translate import scale_view
from .translate import is_container
from .translate import serving_value
from .translate import endpoint_create
from .translate import runtime_request
from .translate import workload_create
from .translate import endpoint_surface
from .translate import endpoint_targets
from .translate import topology_request
from .translate import artifact_manifest
from .translate import endpoint_manifest
from .translate import workload_manifest


class ManifestService:
    def __init__(self, db: AsyncSession, actor: str = "unknown") -> None:
        self.db = db
        self.actor = actor

    async def _project(self, ref: str) -> Project:
        project = await WorkloadRepository(self.db).project_by_ref(ref)
        if project is None:
            raise NotFound(f"no project {ref!r}")
        return project

    async def _names(self, column, uids, *where) -> dict[str, str]:
        model = column.class_
        uids = {u for u in uids if u}
        if not uids:
            return {}
        rows = await self.db.execute(select(model.uid, column).where(model.uid.in_(uids), *where))
        return {uid: name for uid, name in rows.all()}

    async def workload(self, project: str, name: str) -> WorkloadManifest:
        owner = await self._project(project)
        try:
            w = await WorkloadService(self.db).get(name, owner.uid)
        except NotFound:
            raise NotFound(f"no workload {name!r} in project {project!r}") from None
        if w.name != name or w.project_id != owner.uid:
            raise NotFound(f"no workload {name!r} in project {project!r}")

        policy = await self.db.scalar(select(Policy).where(Policy.name == constraint_name(w.id)))
        allowed_ids = list(policy.allowed_cluster_ids) if policy else []
        allowed = {}
        if allowed_ids:
            rows = await self.db.execute(select(Cluster.id, Cluster.name).where(Cluster.id.in_(allowed_ids)))
            allowed = dict(rows.all())
        placement = Placement(
            allowed_regions=list(policy.allowed_regions) if policy else [],
            allowed_clusters=[allowed.get(i, str(i)) for i in allowed_ids],
        )
        note = await self.db.scalar(
            select(WorkloadRevision.note)
            .join(Workload, Workload.id == WorkloadRevision.workload_id)
            .where(Workload.uid == w.id)
            .order_by(WorkloadRevision.number.desc())
            .limit(1)
        )
        refs = Refs(
            projects={owner.uid: owner.slug},
            artifacts=await self._names(
                Artifact.handle, [w.serving.artifact_id, *w.adapter_artifact_ids], visible(owner.id)
            ),
            clusters=await self._names(Cluster.name, [w.pinned_cluster_id, *w.cluster_floors]),
        )
        return workload_manifest(w, refs, placement, note)

    async def _owned_workload(self, project: str, name: str):
        owner = await self._project(project)
        try:
            w = await WorkloadService(self.db).get(name, owner.uid)
        except NotFound:
            raise NotFound(f"no workload {name!r} in project {project!r}") from None
        if w.name != name or w.project_id != owner.uid:
            raise NotFound(f"no workload {name!r} in project {project!r}")
        return owner, w

    async def scale(self, project: str, name: str) -> Scale:
        owner, w = await self._owned_workload(project, name)
        return scale_view(w, owner.slug)

    async def put_scale(self, project: str, name: str, data: Scale, if_match: str | None = None) -> Scale:
        """Writes `spec.replicas` as the workload's `desired_scale` (ADR 0025).

        Spec, so it moves `generation` and honours `If-Match` (or `metadata.resourceVersion`);
        audited, so a change files a succeeded `workload.scale` operation in the same commit.
        A placement shortfall is accepted and reads as an `Unschedulable` condition. Restating
        the current count is a no-op: no generation, no operation, nothing announced.
        """
        owner, w = await self._owned_workload(project, name)
        svc = WorkloadService(self.db)
        row = await svc._one(w.id, lock=True)
        workload = row[0]
        expected = if_match if if_match is not None else data.metadata.resource_version
        revision = _revision(workload)
        if expected is not None and expected != revision:
            raise Conflict("workload changed since it was read", {"revision": revision})
        replicas = data.spec.replicas
        if replicas == workload.desired_scale:
            return scale_view(w, owner.slug)

        # No autoscaler owns the bounds yet, so a manual count widens them (ADR 0025).
        parameters = WorkloadScaleParameters(
            desired=replicas, min=min(workload.min_scale, replicas), max=max(workload.max_scale, replicas)
        )
        now = dt.datetime.now(dt.UTC)
        change = await svc.change_scale(w.id, parameters, refuse=False)
        self.db.add(
            Operation(
                uid=new_uid(OPERATION),
                type="workload.scale",
                target_type="workload",
                target_id=workload.id,
                phase="succeeded",
                requested_by=self.actor,
                reason="PUT /scale",
                payload={"parameters": {"desired": replicas}},
                result=change.result,
                started_at=now,
                finished_at=dt.datetime.now(dt.UTC),
            )
        )
        await self.db.commit()
        await svc.announce(change)
        return scale_view(await svc.get(w.id), owner.slug)

    async def endpoint(self, project: str, name: str) -> EndpointManifest:
        owner = await self._project(project)
        try:
            e = await EndpointService(self.db).get(name, owner.uid)
        except NotFound:
            raise NotFound(f"no endpoint {name!r} in project {project!r}") from None
        if e.name != name or e.project_id != owner.uid:
            raise NotFound(f"no endpoint {name!r} in project {project!r}")
        names = [t.workload_ref.name for t in e.targets if t.workload_ref is not None]
        expanded: dict[str, list[str]] = {}
        if names:
            rows = await self.db.execute(
                select(Workload.name, Deployment.uid)
                .join(Deployment, Deployment.workload_id == Workload.id)
                .where(Workload.project_id == owner.id, Workload.name.in_(names))
                .order_by(Deployment.uid)
            )
            for name, uid in rows.all():
                expanded.setdefault(name, []).append(uid)
        deployments = [t.deployment_id for t in e.targets] + list(e.failover_order)
        deployments += [uid for uids in expanded.values() for uid in uids]
        refs = Refs(
            projects={owner.uid: owner.slug},
            deployments=await self._names(Deployment.object_name, deployments),
            workload_deployments=expanded,
        )
        return endpoint_manifest(e, refs)

    async def artifact(self, project: str | None, name: str) -> ArtifactManifest:
        owner = await self._project(project) if project is not None else None
        where = f"in project {project!r}" if project is not None else "in the public catalogue"
        try:
            a = await ArtifactService(self.db).get(name, project=owner.uid if owner else None)
        except NotFound:
            raise NotFound(f"no artifact {name!r} {where}") from None
        if a.handle != name or a.owner_project_id != (owner.uid if owner else None):
            raise NotFound(f"no artifact {name!r} {where}")
        refs = Refs(
            projects={owner.uid: owner.slug} if owner else {},
            artifacts=await self._names(Artifact.handle, [a.base_artifact_id], visible(owner.id if owner else None)),
        )
        if a.project_id and a.project_id not in refs.projects:
            refs.projects |= await self._names(Project.slug, [a.project_id])
        return artifact_manifest(a, refs)

    # ----------------------------------------------------------------------- PUT (ADR 0029)

    def _identity(self, m, project: str, owner: Project, name: str) -> None:
        if m.metadata.name != name:
            raise Invalid(
                f"metadata.name {m.metadata.name!r} does not match the path's {name!r}",
                {"field": "metadata.name"},
            )
        if m.metadata.namespace is not None and m.metadata.namespace not in (project, owner.slug, owner.uid):
            raise Invalid(
                f"metadata.namespace {m.metadata.namespace!r} does not match the path's {project!r}",
                {"field": "metadata.namespace"},
            )

    @staticmethod
    def _expect(current, if_match: str | None, body_version: str | None) -> str | None:
        expected = if_match if if_match is not None else body_version
        if expected is not None and expected != current.metadata.resource_version:
            raise Conflict(
                f"{current.kind.lower()} changed since it was read",
                {"revision": current.metadata.resource_version},
            )
        return current.metadata.resource_version

    async def _check_artifacts(self, owner: Project, refs: dict[str, str]) -> None:
        """Every artifact the manifest names exists and is visible to its project. No implicit
        create, and another project's private artifact reads as missing."""
        for path, ref in refs.items():
            if await self.db.scalar(visible_ids(ref, owner.id).limit(1)) is None:
                raise Invalid(f"{path}: no artifact {ref!r} visible to project {owner.slug!r}", {"field": path})

    async def put_workload(
        self, project: str, name: str, body: dict, if_match: str | None = None
    ) -> tuple[WorkloadManifest, bool]:
        """Creates or replaces a workload from its manifest. Returns it and whether it was created.

        Replace diffs only what the manifest declares: a field it leaves out keeps the stored
        value, and a declared field equal to it changes nothing, so replaying a manifest moves no
        generation and rolls nothing. Create-only fields (`enabled`) are ignored on replace until
        the last-applied record exists. A placement shortfall is accepted and reads as
        `Unschedulable`. `replicas` left out leaves `desired_scale` where it is, which is also
        what an autoscaler owning the count will need.
        """
        m = _decode(body, WorkloadManifest)
        owner = await self._project(project)
        self._identity(m, project, owner, name)
        if (m.spec.runtime.kind or "dynamo") not in ("dynamo", "container"):
            raise Invalid(f"runtime kind {m.spec.runtime.kind!r} is not supported", {"field": "spec.runtime.kind"})
        if m.spec.runtime.kind == "container" and isinstance(m.spec.runtime.args, dict) and m.spec.runtime.args:
            raise Invalid("spec.runtime.args is a list under kind container", {"field": "spec.runtime.args"})
        if m.spec.runtime.kind != "container" and isinstance(m.spec.runtime.args, list):
            raise Invalid(
                "spec.runtime.args is a map of engine flags under kind dynamo", {"field": "spec.runtime.args"}
            )
        if m.spec.runtime.kind != "container" and (not m.spec.runtime.engine or not m.spec.runtime.version):
            raise Invalid("spec.runtime needs engine and version under kind dynamo", {"field": "spec.runtime"})
        declared = Declared(body)
        try:
            current = await self.workload(project, name)
        except NotFound:
            current = None

        svc = WorkloadService(self.db)
        if current is None:
            await self._check_artifacts(
                owner,
                {"spec.serving.artifact": m.spec.serving.artifact}
                | {f"spec.serving.adapters[{i}]": a for i, a in enumerate(m.spec.serving.adapters)},
            )
            await svc.create(workload_create(m, owner.uid), paused=bool(m.spec.paused))
            return await self.workload(project, name), True

        if any(d.authority != MANAGED for d in current.status.deployments):
            raise Conflict(
                f"workload {name!r} is observed, not managed by the plane; adopt it before applying",
                {"name": name},
            )
        # Hold the row for the whole replace and re-read it, so If-Match is checked against what
        # this transaction writes over, whichever of spec, pause or scale it changes.
        await svc._one(current.metadata.uid, lock=True)
        current = await self.workload(project, name)
        revision = self._expect(current, if_match, m.metadata.resource_version)
        for path in IMMUTABLE_PATHS:
            if declared.has("spec", *path) and _value(m.spec, path) != _value(current.spec, path):
                raise Invalid(f"spec.{_dotted(path)} cannot change after create", {"field": f"spec.{_dotted(path)}"})
        if declared.has("spec", "serving", "adapters") and m.spec.serving.adapters != current.spec.serving.adapters:
            await self._check_artifacts(
                owner, {f"spec.serving.adapters[{i}]": a for i, a in enumerate(m.spec.serving.adapters)}
            )

        def changed(*path: str) -> bool:
            return declared.has(*path) and _value(m, path) != _value(current, path)

        update: dict = {}
        if changed("metadata", "labels"):
            update["labels"] = dict(m.metadata.labels)
        note = m.metadata.annotations.get(NOTE_ANNOTATION)
        if declared.has("metadata", "annotations") and note != current.metadata.annotations.get(NOTE_ANNOTATION):
            update["note"] = note
        for key in ("priority_class", "preemptible", "deadline_at", "overrides"):
            if changed("spec", key):
                update[key] = getattr(m.spec, key)
        for key in ("strategy", "cluster_floors", "max_clusters"):
            if changed("spec", "placement", key):
                update[key] = getattr(m.spec.placement, key)
        if changed("spec", "serving", "adapters"):
            update["adapter_artifacts"] = list(m.spec.serving.adapters)
        container = is_container(m.spec)
        serving = {
            key: serving_value(m.spec, key)
            for key, path in SERVING_FIELDS.items()
            if changed("spec", *path) and not (container and key in DYNAMO_RUNTIME_FIELDS)
        }
        if container and runtime_request(m.spec) != runtime_request(current.spec):
            serving["runtime"] = runtime_request(m.spec)
        if changed("spec", "serving", "topology") or changed("spec", "serving", "roles"):
            serving["topology"] = topology_request(m.spec.serving)
        if serving:
            update["serving"] = WorkloadServingUpdate(**serving)
        changes = []
        if update:
            changes.append(
                await svc.change_update(current.metadata.uid, WorkloadUpdateRequest(**update), if_match=revision)
            )
        if changed("spec", "paused"):
            changes.append(await svc.change_paused(current.metadata.uid, m.spec.paused))
        scale = current.spec.scale
        desired = m.spec.replicas if declared.has("spec", "replicas") and m.spec.replicas is not None else scale.desired
        low = m.spec.scale.min if declared.has("spec", "scale", "min") else scale.min
        high = m.spec.scale.max if declared.has("spec", "scale", "max") else scale.max
        low, high = min(low, desired), max(high, desired)
        if (low, high, desired) != (scale.min, scale.max, scale.desired):
            parameters = WorkloadScaleParameters(min=low, max=high, desired=desired)
            changes.append(await svc.change_scale(current.metadata.uid, parameters, refuse=False))
        if changes:
            await self.db.commit()
            for change in changes:
                await svc.announce(change)
        return await self.workload(project, name), False

    async def put_endpoint(
        self, project: str, name: str, body: dict, if_match: str | None = None
    ) -> tuple[EndpointManifest, bool]:
        """Creates or replaces an endpoint from its manifest, diffing only declared fields.

        A `workloadRef` may name a workload that does not exist yet; it reads as `ResolvedRefs`
        False until it does. A slug another endpoint holds is a 409.
        """
        m = _decode(body, EndpointManifest)
        owner = await self._project(project)
        self._identity(m, project, owner, name)
        declared = Declared(body)
        svc = EndpointService(self.db)
        try:
            current = await self.endpoint(project, name)
        except NotFound:
            current = None

        if current is None:
            created = await svc.create(endpoint_create(m, owner.uid))
            if declared.has("spec", "surface"):
                await svc.update(created.id, EndpointUpdateRequest(surface=endpoint_surface(m.spec)))
            return await self.endpoint(project, name), True

        revision = self._expect(current, if_match, m.metadata.resource_version)

        def changed(*path: str) -> bool:
            return declared.has(*path) and _value(m, path) != _value(current, path)

        update: dict = {}
        if changed("metadata", "labels"):
            update["labels"] = dict(m.metadata.labels)
        for key in ("slug", "visibility", "routing_strategy", "enabled", "aliases"):
            if changed("spec", key):
                update[key] = getattr(m.spec, key)
        if changed("spec", "surface"):
            update["surface"] = endpoint_surface(m.spec)
        if changed("spec", "targets") or changed("spec", "failover_order"):
            spec = m.spec if declared.has("spec", "targets") else current.spec
            update["targets"] = endpoint_targets(spec)
            order = m.spec if declared.has("spec", "failover_order") else current.spec
            update["failover_order"] = list(order.failover_order)
        if update:
            await svc.update(current.metadata.uid, EndpointUpdateRequest(**update), if_match=revision)
        return await self.endpoint(project, name), False


IMMUTABLE_PATHS = (
    ("kind",),
    ("runtime", "kind"),
    ("gang_size",),
    ("cluster",),
    ("placement", "allowed_regions"),
    ("placement", "allowed_clusters"),
    ("serving", "artifact"),
)
"""The `x-immutable` workload fields, plus `serving.artifact`: other weights are a new workload.
`enabled` is create-only and ignored on replace, since a read cannot say what it was."""


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest)


def _dotted(path) -> str:
    return ".".join(_camel(p) for p in path)


def _value(obj, path):
    for attr in path:
        obj = getattr(obj, attr)
    return msgspec.to_builtins(obj)


def _decode(body: dict, cls):
    try:
        return msgspec.convert(body, cls, strict=False)
    except msgspec.ValidationError as exc:
        raise Invalid(f"not a valid {cls.__name__}: {exc}") from exc


class Declared:
    """Which fields a manifest wrote, read from the body as sent, before defaults filled it in.
    Paths are snake_case; the body is camelCase."""

    def __init__(self, body: dict) -> None:
        self.body = body

    def has(self, *path: str) -> bool:
        node = self.body
        for attr in path:
            if not isinstance(node, dict) or _camel(attr) not in node:
                return False
            node = node[_camel(attr)]
        return True
