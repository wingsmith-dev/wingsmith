"""Reads one object as a manifest, through the same service read the HTTP API uses.

Each kind is fetched with its own service's `get`, so visibility is whatever that read enforces,
and then held to the manifest's identity: `name` is the object's name (an Artifact's handle), not
an id, and the object must live in exactly the namespace asked for. An artifact private to one
project is reachable only under that project; a public one only without a namespace.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Policy
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import WorkloadRevision
from app.core.exceptions import NotFound
from app.modules.artifacts.scope import visible
from app.modules.artifacts.services import ArtifactService
from app.modules.endpoints.services import EndpointService
from app.modules.workloads.services import WorkloadService
from app.modules.workloads.services import constraint_name
from app.modules.workloads.repository import WorkloadRepository

from .schemas import ArtifactManifest
from .schemas import EndpointManifest
from .schemas import WorkloadManifest
from .translate import Refs
from .translate import Placement
from .translate import artifact_manifest
from .translate import endpoint_manifest
from .translate import workload_manifest


class ManifestService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

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

    async def endpoint(self, project: str, name: str) -> EndpointManifest:
        owner = await self._project(project)
        try:
            e = await EndpointService(self.db).get(name)
        except NotFound:
            raise NotFound(f"no endpoint {name!r} in project {project!r}") from None
        if e.name != name or e.project_id != owner.uid:
            raise NotFound(f"no endpoint {name!r} in project {project!r}")
        deployments = [t.deployment_id for t in e.targets] + list(e.failover_order)
        refs = Refs(
            projects={owner.uid: owner.slug},
            deployments=await self._names(Deployment.object_name, deployments),
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
