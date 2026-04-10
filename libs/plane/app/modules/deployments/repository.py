from __future__ import annotations

from uuid import UUID

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Replica
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import PriorityClass
from app.db.models import DiscoveredWorkload
from app.core.filters import ref_ids


RUNNING_PHASES = ("starting", "ready", "draining")
"""The observed phases in which a replica's process is actually up.

`running` is not one of the phases a cluster reports — the wire enum is
`pending | starting | ready | draining | terminated | failed` — so counting rows
equal to the string would have counted nothing, forever. A replica that is
draining is still serving what it already accepted, which is why it is here and
`terminated` is not.
"""


class DeploymentRepository:
    """Reads deployments joined to the workload they specialise.

    The join is always present rather than optional: priority, project, and the
    admission phase live on the supertype, and a deployment read without them is
    half an answer.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(
                Deployment,
                Workload,
                Cluster.uid.label("cluster_uid"),
                Recipe.uid.label("recipe_uid"),
                Project.uid.label("project_uid"),
                PriorityClass.name.label("priority_class"),
            )
            .join(Workload, Deployment.workload_id == Workload.id)
            .join(Cluster, Deployment.cluster_id == Cluster.id)
            .join(Recipe, Deployment.recipe_id == Recipe.id)
            .join(Project, Workload.project_id == Project.id)
            .join(PriorityClass, Workload.priority_class_id == PriorityClass.id)
        )

    def get_query(self, ref: str, cluster: str | None = None) -> Select:
        """An id or a name; a name is unique only within its cluster."""
        column = Deployment.uid if is_uid(ref) else Deployment.name
        query = self._base().where(column == ref)
        if cluster is not None:
            query = query.where(Deployment.cluster_id.in_(ref_ids(Cluster, cluster)))
        return query

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        cluster: str | None = None,
        recipe: str | None = None,
        project: str | None = None,
        phase: str | None = None,
        type: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Select:
        query = self._base().order_by(Deployment.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Deployment.seq > after_seq)
        if cluster is not None:
            query = query.where(Deployment.cluster_id.in_(ref_ids(Cluster, cluster)))
        if recipe is not None:
            query = query.where(Deployment.recipe_id.in_(ref_ids(Recipe, recipe)))
        if project is not None:
            query = query.where(Workload.project_id.in_(ref_ids(Project, project, "slug")))
        if phase is not None:
            query = query.where(Workload.admission_phase == phase)
        if type is not None:
            query = query.where(Workload.kind == type)
        for key, value in (labels or {}).items():
            query = query.where(Deployment.labels[key].astext == value)
        return query

    def replica_rollup_query(self, deployment_ids: list) -> Select:
        """Six integers per deployment, counted in the database.

        Walking `Deployment.replicas` would be one query per deployment on a page
        of fifty, and would load every replica row to produce a handful of counts.
        """
        return (
            select(
                Replica.deployment_id,
                func.count(Replica.id).label("total"),
                func.sum(case((Replica.ready.is_(True), 1), else_=0)).label("ready"),
                func.sum(case((Replica.phase.in_(RUNNING_PHASES), 1), else_=0)).label("running"),
                func.sum(case((Replica.phase == "pending", 1), else_=0)).label("pending"),
                func.sum(case((Replica.phase == "failed", 1), else_=0)).label("failed"),
                func.sum(case((Replica.observed_generation == Deployment.generation, 1), else_=0)).label(
                    "at_current_generation"
                ),
            )
            .select_from(Replica)
            .join(Deployment, Replica.deployment_id == Deployment.id)
            .where(Replica.deployment_id.in_(deployment_ids))
            .group_by(Replica.deployment_id)
        )

    def adapter_query(self, artifact_ids: list) -> Select:
        return select(Artifact.id, Artifact.uid).where(Artifact.id.in_(artifact_ids))

    async def get(self, ref: str, cluster: str | None = None) -> Row | None:
        result = await self.db.execute(self.get_query(ref, cluster).limit(1))
        return result.first()

    async def sighting_fingerprint(self, cluster_id: UUID, workload_uid: str) -> str | None:
        """What the cluster currently reports the adopted object is running.

        Read from the sighting rather than from `adopted_fingerprint`, because the
        point of the check is to compare the caller's view against what is live
        now. A sighting that has been pruned returns None, which is a refusal: an
        adopted object the cluster is no longer reporting is not one to take
        management of on the strength of a digest recorded days ago.
        """
        result = await self.db.execute(
            select(DiscoveredWorkload.spec_fingerprint).where(
                DiscoveredWorkload.cluster_id == cluster_id,
                DiscoveredWorkload.uid == workload_uid,
            )
        )
        return result.scalar_one_or_none()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def replicas(self, deployment_ids: list) -> dict:
        rollup: dict = {}
        if not deployment_ids:
            return rollup
        result = await self.db.execute(self.replica_rollup_query(deployment_ids))
        for row in result.all():
            rollup[row.deployment_id] = {
                "total": row.total or 0,
                "ready": row.ready or 0,
                "running": row.running or 0,
                "pending": row.pending or 0,
                "failed": row.failed or 0,
                "at_current_generation": row.at_current_generation or 0,
            }
        return rollup

    async def adapter_uids(self, artifact_ids: list) -> dict[UUID, str]:
        if not artifact_ids:
            return {}
        result = await self.db.execute(self.adapter_query(artifact_ids))
        return {row.id: row.uid for row in result.all()}

    async def cluster_by_ref(self, ref: str) -> Cluster | None:
        column = Cluster.uid if is_uid(ref) else Cluster.name
        result = await self.db.execute(select(Cluster).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def recipe_by_ref(self, ref: str) -> Recipe | None:
        column = Recipe.uid if is_uid(ref) else Recipe.name
        result = await self.db.execute(select(Recipe).where(column == ref).order_by(Recipe.version.desc()).limit(1))
        return result.scalar_one_or_none()

    async def project_by_ref(self, ref: str) -> Project | None:
        column = Project.uid if is_uid(ref) else Project.slug
        result = await self.db.execute(select(Project).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def priority_class_by_name(self, name: str) -> PriorityClass | None:
        result = await self.db.execute(select(PriorityClass).where(PriorityClass.name == name).limit(1))
        return result.scalar_one_or_none()

    async def default_priority_class(self) -> PriorityClass | None:
        result = await self.db.execute(
            select(PriorityClass)
            .where(PriorityClass.is_default.is_(True))
            .order_by(PriorityClass.value.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def priority_class_name(self, priority_class_id: UUID) -> str | None:
        result = await self.db.execute(select(PriorityClass.name).where(PriorityClass.id == priority_class_id))
        return result.scalar_one_or_none()

    async def artifacts_by_ref(self, refs: list[str]) -> list[Artifact]:
        found: list[Artifact] = []
        for ref in refs:
            column = Artifact.uid if is_uid(ref) else Artifact.name
            result = await self.db.execute(select(Artifact).where(column == ref).limit(1))
            artifact = result.scalar_one_or_none()
            if artifact is not None:
                found.append(artifact)
        return found

    async def add(self, workload: Workload, deployment: Deployment) -> Deployment:
        self.db.add(workload)
        self.db.add(deployment)
        await self.db.flush()
        return deployment

    async def delete(self, workload: Workload, deployment: Deployment) -> None:
        """Deletes the specialisation and then the supertype it hangs off.

        Deleting the workload alone would leave the ORM holding a loaded
        `Workload.deployment`, which it detaches by nulling `workload_id` before
        issuing the delete — a not-null column, so the delete fails on its way
        out. The database would cascade either way; the order is what keeps the
        unit of work from writing an impossible row first.
        """
        await self.db.delete(deployment)
        await self.db.delete(workload)
        await self.db.flush()

    async def priority_class(self, priority_class_id: UUID) -> PriorityClass | None:
        return await self.db.get(PriorityClass, priority_class_id)
