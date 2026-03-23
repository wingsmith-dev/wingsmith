from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.ids import is_uid
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import PriorityClass
from app.db.models import WorkloadAdoption
from app.db.models import DiscoveredWorkload
from app.core.filters import ref_ids


ADOPTION_CONSTRAINT = "uq_demand_workload_adoption_identity"


class DiscoveredWorkloadRepository:
    """Reads sightings, writes decisions. Never the other way round.

    The sighting half is observed state and belongs to ingestion; the only rows
    this repository writes are in `demand_workload_adoption`, which ingestion
    never touches.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        """The sighting, the decision beside it, and the deployment it became.

        `promoted` is not a stored status. It is the presence of a deployment
        pointing back at this sighting, which is one fact in one place rather than
        a column that has to be kept in agreement with the deployments table.
        """
        return (
            select(DiscoveredWorkload, Cluster.uid, Cluster.name, WorkloadAdoption, Project.uid, Deployment.uid)
            .join(Cluster, DiscoveredWorkload.cluster_id == Cluster.id)
            .outerjoin(
                WorkloadAdoption,
                (WorkloadAdoption.cluster_id == DiscoveredWorkload.cluster_id)
                & (WorkloadAdoption.workload_uid == DiscoveredWorkload.uid),
            )
            .outerjoin(Project, WorkloadAdoption.project_id == Project.id)
            .outerjoin(
                Deployment,
                (Deployment.cluster_id == DiscoveredWorkload.cluster_id)
                & (Deployment.adopted_workload_uid == DiscoveredWorkload.uid),
            )
        )

    async def get(self, ref: str, cluster: str | None = None) -> Row | None:
        column = DiscoveredWorkload.uid if is_uid(ref) else DiscoveredWorkload.name
        query = self._base().where(column == ref)
        if cluster is not None:
            query = query.where(DiscoveredWorkload.cluster_id.in_(ref_ids(Cluster, cluster)))
        result = await self.db.execute(query)
        return result.first()

    def query(
        self,
        limit: int,
        after_seq: int | None = None,
        cluster: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> Select:
        query = self._base().order_by(DiscoveredWorkload.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(DiscoveredWorkload.seq > after_seq)
        if cluster is not None:
            query = query.where(DiscoveredWorkload.cluster_id.in_(ref_ids(Cluster, cluster)))
        if namespace is not None:
            query = query.where(DiscoveredWorkload.namespace == namespace)
        if status == "new":
            query = query.where(WorkloadAdoption.id.is_(None))
        elif status == "promoted":
            query = query.where(Deployment.id.is_not(None))
        elif status is not None:
            query = query.where(WorkloadAdoption.decision == status, Deployment.id.is_(None))
        if search is not None:
            query = query.where(DiscoveredWorkload.name.ilike(f"%{search}%"))
        return query

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.query(**kwargs))
        return list(result.all())

    async def project_by_ref(self, ref: str) -> Row | None:
        result = await self.db.execute(ref_ids(Project, ref).with_only_columns(Project.id, Project.uid))
        return result.first()

    async def decide(self, values: dict) -> None:
        """Upserts the decision. One decision per workload, last one wins."""
        statement = pg_insert(WorkloadAdoption).values(**values)
        await self.db.execute(
            statement.on_conflict_do_update(
                constraint=ADOPTION_CONSTRAINT,
                set_={key: statement.excluded[key] for key in values if key not in {"cluster_id", "workload_uid"}},
            )
        )
        await self.db.flush()

    async def recipe_by_ref(self, ref: str) -> Row | None:
        """The newest revision of a recipe named by name, or the one named by id."""
        column = Recipe.uid if is_uid(ref) else Recipe.name
        result = await self.db.execute(
            select(Recipe.id, Recipe.uid).where(column == ref).order_by(Recipe.version.desc()).limit(1)
        )
        return result.first()

    async def priority_class_by_name(self, name: str):
        result = await self.db.execute(select(PriorityClass).where(PriorityClass.name == name))
        return result.scalar_one_or_none()

    async def default_priority_class(self):
        result = await self.db.execute(
            select(PriorityClass)
            .where(PriorityClass.is_default.is_(True))
            .order_by(PriorityClass.value.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def project_row(self, ref: str) -> Project | None:
        """A project is named by its slug, not by its display name, as everywhere else."""
        column = Project.uid if is_uid(ref) else Project.slug
        result = await self.db.execute(select(Project).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def add(self, workload: Workload, deployment: Deployment) -> None:
        self.db.add(workload)
        self.db.add(deployment)
        await self.db.flush()
