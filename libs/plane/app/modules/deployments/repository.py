from __future__ import annotations

from uuid import UUID

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.ids import is_uid
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Replica
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import DeploymentStatus
from app.db.models import DiscoveredWorkload
from app.core.lookup import at_most_one
from app.core.filters import ref_ids


RUNNING_PHASES = ("starting", "ready", "draining")
"""The observed phases in which a replica's process is actually up.

`running` is not one of the phases a cluster reports — the wire enum is
`pending | starting | ready | draining | terminated | failed` — so counting rows
equal to the string would have counted nothing, forever. A replica that is
draining is still serving what it already accepted, which is why it is here and
`terminated` is not.
"""


async def bump_generation(db: AsyncSession, deployment: Deployment) -> int:
    """Increments a deployment's generation in the database and returns the new value.

    `UPDATE ... SET generation = generation + 1 RETURNING` rather than `+= 1` on the
    loaded row, so two writers that both read generation n write n+1 and n+2 instead
    of both writing n+1, which would leave a cluster that saw the first write
    believing it is already current with the second. Pending column changes are
    flushed first so they land in the same row version.
    """
    await db.flush()
    row = (
        await db.execute(
            update(Deployment)
            .where(Deployment.id == deployment.id)
            .values(generation=Deployment.generation + 1)
            .returning(Deployment.generation, Deployment.updated_at)
            .execution_options(synchronize_session=False)
        )
    ).one()
    set_committed_value(deployment, "generation", row.generation)
    set_committed_value(deployment, "updated_at", row.updated_at)
    return row.generation


class DeploymentRepository:
    """Reads deployments joined to the workload they belong to.

    The join is many-to-one now rather than one-to-one: a workload has one
    deployment per cluster it was expanded onto, and this module reads a single
    one of them. It is still always present, because a deployment carries only
    this cluster's share and its admission — what was actually asked for is on
    the parent, and a deployment read without at least its name is half an answer.

    The observed status is an outer join because it is written by the cluster and
    a deployment that no cluster has reported on yet is the normal state for the
    first seconds of its life — and the permanent state of one whose cluster is
    unreachable. An inner join would make those deployments vanish from the list
    rather than show up without a status, which is the opposite of what somebody
    looking for a stuck deployment needs.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(
                Deployment,
                Workload.uid.label("workload_uid"),
                Workload.name.label("workload_name"),
                Cluster.uid.label("cluster_uid"),
                DeploymentStatus,
            )
            .join(Workload, Deployment.workload_id == Workload.id)
            .outerjoin(DeploymentStatus, DeploymentStatus.deployment_id == Deployment.id)
            .join(Cluster, Deployment.cluster_id == Cluster.id)
        )

    def get_query(self, ref: str, cluster: str | None = None) -> Select:
        """An id or a name; a name is unique only within its cluster.

        A workload expanded onto three clusters has three deployments sharing one
        name, so a bare name is ambiguous across clusters by construction and the
        `cluster` filter is what makes it an answer.
        """
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
        workload: str | None = None,
        project: str | None = None,
        phase: str | None = None,
    ) -> Select:
        query = self._base().order_by(Deployment.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Deployment.seq > after_seq)
        if cluster is not None:
            query = query.where(Deployment.cluster_id.in_(ref_ids(Cluster, cluster)))
        if workload is not None:
            query = query.where(Deployment.workload_id.in_(ref_ids(Workload, workload)))
        if project is not None:
            query = query.where(Workload.project_id.in_(ref_ids(Project, project, "slug")))
        if phase is not None:
            query = query.where(Deployment.admission_phase == phase)
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

    async def get(self, ref: str, cluster: str | None = None, lock: bool = False) -> Row | None:
        """By id, or by name narrowed by `cluster`.

        A workload expanded onto three clusters has three deployments of one name, so a
        bare name that matches more than one is refused rather than resolved to any of
        them. `lock` holds the deployment row until the transaction ends, re-read, for a write.
        """
        query = self.get_query(ref, cluster).limit(2)
        if lock:
            query = query.with_for_update(of=Deployment).execution_options(populate_existing=True)
        result = await self.db.execute(query)
        return at_most_one(list(result.all()), "deployment", ref, "use its id or name a cluster")

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

    async def cluster_by_ref(self, ref: str) -> Cluster | None:
        column = Cluster.uid if is_uid(ref) else Cluster.name
        result = await self.db.execute(select(Cluster).where(column == ref).limit(1))
        return result.scalar_one_or_none()
