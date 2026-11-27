"""Reads one object as a manifest, through the same service read the HTTP API uses.

Each kind is fetched with its own service's `get`, so visibility is whatever that read enforces,
and then held to the manifest's identity: `name` is the object's name (an Artifact's handle), not
an id, and the object must live in exactly the namespace asked for. An artifact private to one
project is reachable only under that project; a public one only without a namespace.
"""

import copy
import json
import typing
import datetime as dt
import contextlib

import msgspec

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import OPERATION
from app.core.ids import new_uid
from app.db.models import Policy
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Operation
from app.db.models import Deployment
from app.db.models import WorkloadRevision
from app.core.ownership import DEFAULT_MANAGER
from app.core.ownership import guard
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

    async def _applied_state(self, model, uid: str):
        return (
            await self.db.execute(
                select(model.last_applied, model.applied_by, model.applied_by_principal, model.applied_at).where(
                    model.uid == uid
                )
            )
        ).one()

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
        return with_last_applied(workload_manifest(w, refs, placement, note), await self._applied_state(Workload, w.id))

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
        return with_last_applied(endpoint_manifest(e, refs), await self._applied_state(Endpoint, e.id))

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

    async def _hold(self, *services):
        """Runs a write with every service commit made a flush and every announcement held back,
        so create, replace and the last-applied record land in one transaction. `release` commits
        and then announces; `discard` rolls back and announces nothing (`dryRun=All`)."""
        held: list = []
        for svc in services:
            original = svc._announce

            async def defer(*args, original=original):
                held.append((original, args))

            svc._announce = defer
        self.db.commit = self.db.flush
        hold = _Hold(self.db, held)
        try:
            yield hold
        finally:
            del self.db.commit
            for svc in services:
                del svc._announce

    _hold = contextlib.asynccontextmanager(_hold)

    async def _record(self, model, uid: str, body: dict, manager: str) -> None:
        await self.db.execute(
            update(model)
            .where(model.uid == uid)
            .values(
                last_applied=_applied(body),
                applied_by=manager,
                applied_by_principal=self.actor,
                applied_at=dt.datetime.now(dt.UTC),
            )
        )

    async def put_workload(
        self,
        project: str,
        name: str,
        body: dict,
        if_match: str | None = None,
        dry_run: bool = False,
        manager: str = DEFAULT_MANAGER,
        force: bool = False,
    ) -> tuple[WorkloadManifest, str]:
        """Creates or replaces a workload from its manifest. Returns it and `created`,
        `configured` or `unchanged` (ADR 0029, 0032).

        Replace diffs what the manifest declares, three-way against the last-applied record: a
        field the previous apply declared and this one leaves out goes back to its default
        (`x-defaulted` ones keep the stored value; an `x-immutable` one whose default differs from the stored value is a 422), and any other
        undeclared field keeps the stored value. Without a record, only declared fields count.
        A declared field equal to the stored value changes nothing, so a replay moves no
        generation and rolls nothing. The body is then stored as the record, in the same
        transaction. `dry_run` does all of it and rolls back.
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
        try:
            current = await self.workload(project, name)
        except NotFound:
            current = None

        svc = WorkloadService(self.db)
        async with self._hold(svc) as hold:
            if current is None:
                await self._check_artifacts(
                    owner,
                    {"spec.serving.artifact": m.spec.serving.artifact}
                    | {f"spec.serving.adapters[{i}]": a for i, a in enumerate(m.spec.serving.adapters)},
                )
                created = await svc.create(workload_create(m, owner.uid), paused=bool(m.spec.paused))
                await self._record(Workload, created.id, body, manager)
                return await hold.done(self.workload(project, name), dry_run), "created"

            if any(d.authority != MANAGED for d in current.status.deployments):
                raise Invalid(f"workload {name!r} is observed, adopt it first", {"name": name, "reason": "observed"})
            # Hold the row for the whole replace and re-read it, so If-Match is checked against what
            # this transaction writes over, whichever of spec, pause or scale it changes.
            await svc._one(current.metadata.uid, lock=True)
            await guard(
                self.db, Workload, current.metadata.uid, kind="workload", actor=self.actor, force=force, manager=manager
            )
            current = await self.workload(project, name)
            revision = self._expect(current, if_match, m.metadata.resource_version)
            exempt = {("spec", "replicas")} if autoscaled(current) else set()
            merged = merge_removed(body, last_applied(current), WorkloadManifest, exempt)
            m = _decode(merged, WorkloadManifest)
            declared = Declared(merged)
            for path in IMMUTABLE_PATHS:
                if declared.has("spec", *path) and _value(m.spec, path) != _value(current.spec, path):
                    raise Invalid(
                        f"spec.{_dotted(path)} cannot change after create", {"field": f"spec.{_dotted(path)}"}
                    )
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
            desired = (
                m.spec.replicas if declared.has("spec", "replicas") and m.spec.replicas is not None else scale.desired
            )
            low = m.spec.scale.min if declared.has("spec", "scale", "min") else scale.min
            high = m.spec.scale.max if declared.has("spec", "scale", "max") else scale.max
            low, high = min(low, desired), max(high, desired)
            if (low, high, desired) != (scale.min, scale.max, scale.desired):
                parameters = WorkloadScaleParameters(min=low, max=high, desired=desired)
                changes.append(await svc.change_scale(current.metadata.uid, parameters, refuse=False))
            for change in changes:
                await svc.announce(change)
            await self._record(Workload, current.metadata.uid, body, manager)
            after = await hold.done(self.workload(project, name), dry_run)
        return after, _result(current, after)

    async def put_endpoint(
        self,
        project: str,
        name: str,
        body: dict,
        if_match: str | None = None,
        dry_run: bool = False,
        manager: str = DEFAULT_MANAGER,
        force: bool = False,
    ) -> tuple[EndpointManifest, str]:
        """Creates or replaces an endpoint from its manifest, three-way against the last-applied
        record like a workload.

        A `workloadRef` may name a workload that does not exist yet; it reads as `ResolvedRefs`
        False until it does. A slug another endpoint holds is a 409.
        """
        m = _decode(body, EndpointManifest)
        owner = await self._project(project)
        self._identity(m, project, owner, name)
        svc = EndpointService(self.db)
        try:
            current = await self.endpoint(project, name)
        except NotFound:
            current = None

        async with self._hold() as hold:
            if current is None:
                created = await svc.create(endpoint_create(m, owner.uid))
                if Declared(body).has("spec", "surface"):
                    await svc.update(created.id, EndpointUpdateRequest(surface=endpoint_surface(m.spec)))
                await self._record(Endpoint, created.id, body, manager)
                return await hold.done(self.endpoint(project, name), dry_run), "created"

            await svc._one(current.metadata.uid, lock=True)
            await guard(
                self.db, Endpoint, current.metadata.uid, kind="endpoint", actor=self.actor, force=force, manager=manager
            )
            current = await self.endpoint(project, name)
            revision = self._expect(current, if_match, m.metadata.resource_version)
            merged = merge_removed(body, last_applied(current), EndpointManifest)
            m = _decode(merged, EndpointManifest)
            declared = Declared(merged)

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
            await self._record(Endpoint, current.metadata.uid, body, manager)
            after = await hold.done(self.endpoint(project, name), dry_run)
        return after, _result(current, after)


class _Hold:
    def __init__(self, db: AsyncSession, held: list) -> None:
        self.db = db
        self.held = held

    async def done(self, read, dry_run: bool):
        """Reads the would-be object, then commits and announces, or rolls back."""
        out = await read
        if dry_run:
            await self.db.rollback()
            self.held.clear()
            return out
        await AsyncSession.commit(self.db)
        for announce, args in self.held:
            await announce(*args)
        self.held.clear()
        return out


LAST_APPLIED = "plane.wingsmith.dev/last-applied"
"""The body the last apply sent, as compact JSON, like `kubectl.kubernetes.io/last-applied-configuration`."""
APPLIED_BY = "plane.wingsmith.dev/applied-by"
APPLIED_BY_PRINCIPAL = "plane.wingsmith.dev/applied-by-principal"
APPLIED_AT = "plane.wingsmith.dev/applied-at"
PLANE_ANNOTATIONS = frozenset({LAST_APPLIED, APPLIED_BY, APPLIED_BY_PRINCIPAL, APPLIED_AT})
"""Written by the plane on read, never taken from a body (ADR 0032, 0033)."""

_READ_ONLY_META = ("uid", "generation", "creationTimestamp", "resourceVersion")


def _applied(body: dict) -> dict:
    """The body as the record keeps it: no status, no read-only metadata, no record of a record."""
    out = {k: v for k, v in body.items() if k != "status"}
    meta = {k: v for k, v in (out.get("metadata") or {}).items() if k not in _READ_ONLY_META}
    if isinstance(meta.get("annotations"), dict):
        meta["annotations"] = {k: v for k, v in meta["annotations"].items() if k not in PLANE_ANNOTATIONS}
    out["metadata"] = meta
    return out


def last_applied(m) -> dict | None:
    raw = m.metadata.annotations.get(LAST_APPLIED)
    return json.loads(raw) if raw else None


def with_last_applied(m, state):
    """The plane's own annotations: the last-applied record and who applied it (ADR 0032, 0033)."""
    extra = {}
    if state.last_applied is not None:
        extra[LAST_APPLIED] = json.dumps(state.last_applied, sort_keys=True, separators=(",", ":"))
    if state.applied_by is not None:
        extra[APPLIED_BY] = state.applied_by
        if state.applied_by_principal:
            extra[APPLIED_BY_PRINCIPAL] = state.applied_by_principal
        if state.applied_at is not None:
            extra[APPLIED_AT] = state.applied_at.isoformat().replace("+00:00", "Z")
    if extra:
        m.metadata.annotations = m.metadata.annotations | extra
    return m


def autoscaled(current: WorkloadManifest) -> bool:
    """Whether an autoscaler owns `spec.replicas`, which an apply then never unsets (HPA rule).
    No autoscaler exists yet (P1 decision 5), so nothing is."""
    return False


def _result(before, after) -> str:
    def seen(m):
        notes = {k: v for k, v in m.metadata.annotations.items() if k not in PLANE_ANNOTATIONS}
        return m.metadata.generation, m.metadata.labels, notes

    return "unchanged" if seen(before) == seen(after) else "configured"


_SKIP_REMOVED = {("spec", "enabled"), ("status",)}
"""Never reset by removal: `enabled` is create-only and a read cannot say what it was."""


def merge_removed(body: dict, last: dict | None, cls, exempt=frozenset()) -> dict:
    """The body with every field the last apply declared and this one leaves out set to its
    default, as if declared. `x-defaulted` fields and `exempt` paths are left out instead, so
    they keep the stored value. An `x-immutable` one is reset too, so the immutable check
    refuses it (422) unless the default is what is stored. Maps are user data and are
    replaced whole, so only struct fields are walked."""
    body = _applied(body)
    if not last:
        return body
    out = copy.deepcopy(body)
    model = _decode(out, cls)

    def walk(prev: dict, node: dict, obj, path: tuple) -> None:
        fields = {f.encode_name: f for f in msgspec.structs.fields(type(obj))}
        hints = typing.get_type_hints(type(obj), include_extras=True)
        for key, was in prev.items():
            f = fields.get(key)
            if f is None:
                continue
            here = (*path, f.name)
            if here in exempt or here in _SKIP_REMOVED:
                continue
            if key not in node:
                marks = _marks(hints[f.name])
                if "x-defaulted" in marks:
                    continue
                node[key] = msgspec.to_builtins(getattr(obj, f.name))
                continue
            child = getattr(obj, f.name)
            if isinstance(child, msgspec.Struct) and isinstance(was, dict) and isinstance(node[key], dict):
                walk(was, node[key], child, here)

    walk(last, out, model, ())
    return out


def _marks(hint) -> set[str]:
    out = set()
    for meta in getattr(hint, "__metadata__", ()):
        out |= set((getattr(meta, "extra_json_schema", None) or {}).keys())
    return out


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
