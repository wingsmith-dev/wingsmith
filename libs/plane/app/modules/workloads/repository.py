from __future__ import annotations

from uuid import UUID
from collections.abc import Sequence

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Replica
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Allocation
from app.db.models import Deployment
from app.db.models import WorkloadRole
from app.db.models import PriorityClass
from app.db.models import AllocationDevice
from app.db.models import DeploymentStatus
from app.core.filters import ref_ids


RUNNING_PHASES = ("starting", "ready", "draining")
"""The observed phases in which a replica's process is actually up.

`running` is not one of the phases a cluster reports — the wire enum is
`pending | starting | ready | draining | terminated | failed` — so counting rows
equal to the string would have counted nothing, forever. A replica that is
draining is still serving what it already accepted, which is why it is here and
`terminated` is not.
"""


class WorkloadRepository:
    """Reads workloads, and their per-cluster expansion in one grouped query.

    `Workload.deployments` is `lazy="noload"`, and a page of workloads walked
    through the relationship would be one round trip per row — three, once the
    cluster and the observed status are wanted with it. So the deployments for a
    whole page are fetched at once and keyed by workload, and the replica counts
    for all of them at once after that.

    Project and priority class are joined rather than loaded because a workload read
    without them is half an answer: they are who asked for it and at what priority.

    The artifact is `selectinload`ed rather than joined and selected beside its own
    labels, because the read needs the entity and not a column of it: the served
    model name and the serving digest are both resolved from the whole object — see
    `app.core.serving` — and a label per field they read is one more thing to keep in
    step every time that order changes.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(
                Workload,
                Project.uid.label("project_uid"),
                PriorityClass.name.label("priority_class"),
                Cluster.uid.label("pinned_cluster_uid"),
            )
            .join(Project, Workload.project_id == Project.id)
            .join(PriorityClass, Workload.priority_class_id == PriorityClass.id)
            .outerjoin(Cluster, Workload.pinned_cluster_id == Cluster.id)
            .options(selectinload(Workload.artifact))
        )

    def placement_query(self, workload_id) -> Select:
        """Every live device a workload currently holds, with node and replica.

        Scoped to live allocations and live members — `released_at IS NULL` on
        both — because placement is where the workload is now, not where it has
        been. One row per held device, joined out to the node it sits on and the
        replica the allocation serves, so the rollup is assembled in one round
        trip rather than one per allocation.
        """
        return (
            select(
                Allocation.uid.label("allocation_uid"),
                Allocation.device_count.label("device_count"),
                Cluster.uid.label("cluster_uid"),
                Replica.uid.label("replica_uid"),
                Device.uid.label("device_uid"),
                Node.uid.label("node_uid"),
            )
            .select_from(Allocation)
            .join(Cluster, Allocation.cluster_id == Cluster.id)
            .outerjoin(Replica, Allocation.replica_id == Replica.id)
            .join(AllocationDevice, AllocationDevice.allocation_id == Allocation.id)
            .join(Device, AllocationDevice.device_id == Device.id)
            .join(Node, Device.node_id == Node.id)
            .where(
                Allocation.workload_id == workload_id,
                Allocation.released_at.is_(None),
                AllocationDevice.released_at.is_(None),
            )
            .order_by(Allocation.seq.asc(), Node.name.asc())
        )

    async def placement(self, workload_id) -> list[Row]:
        result = await self.db.execute(self.placement_query(workload_id))
        return list(result.all())

    def get_query(self, ref: str, project: str | None = None) -> Select:
        """An id, or a name that is unique only within its project."""
        column = Workload.uid if is_uid(ref) else Workload.name
        query = self._base().where(column == ref)
        if project is not None:
            query = query.where(Workload.project_id.in_(ref_ids(Project, project, "slug")))
        return query

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        project: str | None = None,
        kind: str | None = None,
        phase: str | None = None,
        artifact: str | None = None,
        serving_digest: str | None = None,
        device_class: str | None = None,
        cluster: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Select:
        """One page of workloads, narrowed by whatever the caller stated.

        `serving_digest` is an exact equality on the whole `svg_...` string and is
        never a prefix match, because a prefix of a hash means nothing. It is the
        replacement for the console's `matchExistingRecipe`, which compared eight
        fields in the browser to decide whether an existing recipe could be reused
        and got it wrong whenever a ninth had been hand-tuned. "Does this
        configuration already exist" is now one indexed equality test, asked of the
        plane rather than reconstructed from a listing.

        `device_class` answers "what is asking for H100s", which is a capacity
        question a person genuinely has and which nothing could answer while the
        shape lived on a separate object. Both columns are indexed.
        """
        query = self._base().order_by(Workload.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Workload.seq > after_seq)
        if project is not None:
            query = query.where(Workload.project_id.in_(ref_ids(Project, project, "slug")))
        if kind is not None:
            query = query.where(Workload.kind == kind)
        if artifact is not None:
            query = query.where(Workload.artifact_id.in_(ref_ids(Artifact, artifact)))
        if serving_digest is not None:
            query = query.where(Workload.serving_digest == serving_digest)
        if device_class is not None:
            query = query.where(Workload.device_class == device_class)
        if cluster is not None:
            query = query.where(
                select(Deployment.id)
                .where(
                    Deployment.workload_id == Workload.id,
                    Deployment.cluster_id.in_(ref_ids(Cluster, cluster)),
                )
                .exists()
            )
        if phase is not None:
            query = query.where(
                select(Deployment.id)
                .where(Deployment.workload_id == Workload.id, Deployment.admission_phase == phase)
                .exists()
            )
        for key, value in (labels or {}).items():
            query = query.where(Workload.labels[key].astext == value)
        return query

    def deployment_query(self, workload_ids: list) -> Select:
        return (
            select(
                Deployment,
                Cluster.uid.label("cluster_uid"),
                Cluster.name.label("cluster_name"),
                Cluster.region.label("region"),
                DeploymentStatus,
            )
            .join(Cluster, Deployment.cluster_id == Cluster.id)
            .outerjoin(DeploymentStatus, DeploymentStatus.deployment_id == Deployment.id)
            .where(Deployment.workload_id.in_(workload_ids))
            .order_by(Cluster.name.asc())
        )

    def replica_rollup_query(self, deployment_ids: list) -> Select:
        """Six integers per deployment, counted in the database."""
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

    async def matches(self, ref: str, project: str | None = None) -> list[Row]:
        """Every workload a reference could mean, capped at two.

        A name is unique per project, so a bare name with no project filter can
        legitimately name several workloads. Two rows is enough to tell "one" from
        "more than one"; the caller asks for the full list only in the second case.
        """
        result = await self.db.execute(self.get_query(ref, project).limit(2))
        return list(result.all())

    async def candidates(self, name: str) -> list[Row]:
        """Every workload answering to a bare name, with the project it is in.

        Only consulted when a name resolved to more than one row: the caller owes
        the client the list of projects it could have meant.
        """
        result = await self.db.execute(
            select(Workload.uid, Project.slug)
            .join(Project, Workload.project_id == Project.id)
            .where(Workload.name == name)
        )
        return list(result.all())

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def deployments(self, workload_ids: list) -> dict[UUID, list[Row]]:
        grouped: dict[UUID, list[Row]] = {}
        if not workload_ids:
            return grouped
        result = await self.db.execute(self.deployment_query(workload_ids))
        for row in result.all():
            grouped.setdefault(row[0].workload_id, []).append(row)
        return grouped

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
        result = await self.db.execute(select(Artifact.id, Artifact.uid).where(Artifact.id.in_(artifact_ids)))
        return {row.id: row.uid for row in result.all()}

    async def artifacts_by_ref(self, refs: list[str]) -> list[Artifact]:
        found: list[Artifact] = []
        for ref in refs:
            column = Artifact.uid if is_uid(ref) else Artifact.name
            result = await self.db.execute(select(Artifact).where(column == ref).limit(1))
            artifact = result.scalar_one_or_none()
            if artifact is not None:
                found.append(artifact)
        return found

    async def cluster_by_ref(self, ref: str) -> Cluster | None:
        column = Cluster.uid if is_uid(ref) else Cluster.name
        result = await self.db.execute(select(Cluster).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def artifact_by_ref(self, ref: str) -> Artifact | None:
        column = Artifact.uid if is_uid(ref) else Artifact.name
        result = await self.db.execute(select(Artifact).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def artifact_of(self, workload: Workload) -> Artifact | None:
        """The artifact a workload serves, by primary key.

        A read already carries the entity through `selectinload`, but an update
        arrives at the row through `_one` and then has to re-check the device shape
        against the weights before it writes — this fetches it once, by key, rather
        than depending on how the caller got here.
        """
        return await self.db.get(Artifact, workload.artifact_id)

    async def project_by_ref(self, ref: str) -> Project | None:
        column = Project.uid if is_uid(ref) else Project.slug
        result = await self.db.execute(select(Project).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def priority_class_by_name(self, name: str) -> PriorityClass | None:
        result = await self.db.execute(select(PriorityClass).where(PriorityClass.name == name).limit(1))
        return result.scalar_one_or_none()

    async def priority_class(self, priority_class_id: UUID) -> PriorityClass | None:
        return await self.db.get(PriorityClass, priority_class_id)

    async def default_priority_class(self) -> PriorityClass | None:
        result = await self.db.execute(
            select(PriorityClass)
            .where(PriorityClass.is_default.is_(True))
            .order_by(PriorityClass.value.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def add(
        self, workload: Workload, deployments: list[Deployment], roles: Sequence[WorkloadRole] = ()
    ) -> Workload:
        self.db.add(workload)
        for deployment in deployments:
            self.db.add(deployment)
        await self.db.flush()
        for role in roles:
            role.workload_id = workload.id
            self.db.add(role)
        if roles:
            await self.db.flush()
        return workload

    async def roles_of(self, workload: Workload) -> list[WorkloadRole]:
        """This workload's role rows, ordered so two reads of one workload agree.

        A query rather than `workload.roles`, which is `lazy="noload"` and answers an empty list
        rather than raising — the same trap `serving_digest` documents. Every caller that needs
        the rows asks for them here, so there is one place that can be wrong.
        """
        result = await self.db.execute(
            select(WorkloadRole).where(WorkloadRole.workload_id == workload.id).order_by(WorkloadRole.role)
        )
        return list(result.scalars())

    async def roles_for(self, workload_ids: Sequence[UUID]) -> dict[UUID, list[WorkloadRole]]:
        """The role rows for a page of workloads, in one round trip.

        The same argument the class docstring makes about deployments: a page walked through the
        relationship is one query per row, and a list endpoint that reports a ratio would pay it
        for every workload on the page including the aggregated ones that have no rows at all.
        """
        if not workload_ids:
            return {}
        result = await self.db.execute(
            select(WorkloadRole).where(WorkloadRole.workload_id.in_(workload_ids)).order_by(WorkloadRole.role)
        )
        grouped: dict[UUID, list[WorkloadRole]] = {}
        for role in result.scalars():
            grouped.setdefault(role.workload_id, []).append(role)
        return grouped

    async def replace_roles(self, workload: Workload, roles: Sequence[WorkloadRole]) -> list[WorkloadRole]:
        """Sets a workload's roles to exactly these, deleting whatever was there.

        Replace rather than merge, matching how the serving request treats every other block it
        carries: "the sub-blocks replace whole … a patch that named half of one would leave the
        rest at its defaults rather than at what was stored, and the caller could not tell which
        it got." A ratio is exactly that kind of block — 1:2 and 2:1 share a role name and mean
        opposite things, so a merge keyed on the name would silently keep half of the old ratio.
        """
        await self.db.execute(delete(WorkloadRole).where(WorkloadRole.workload_id == workload.id))
        stored = []
        for role in roles:
            role.workload_id = workload.id
            self.db.add(role)
            stored.append(role)
        await self.db.flush()
        return stored

    async def delete(self, workload: Workload, deployments: list[Deployment]) -> None:
        """Deletes the expansion and then the workload it hangs off.

        The database would cascade either way. The order is what keeps the unit of
        work from first detaching a loaded deployment by nulling its not-null
        `workload_id` on its way out.
        """
        for deployment in deployments:
            await self.db.delete(deployment)
        await self.db.delete(workload)
        await self.db.flush()

    async def clusters_by_uid(self, uids: list[str]) -> dict[str, Cluster]:
        if not uids:
            return {}
        result = await self.db.execute(select(Cluster).where(Cluster.uid.in_(uids)))
        return {c.uid: c for c in result.scalars()}
