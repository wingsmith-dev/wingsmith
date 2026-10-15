from collections.abc import Sequence

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Deployment
from app.db.models import PriorityClass
from app.db.models import ArtifactLocation
from app.db.models import WorkloadAdoption
from app.db.models import DiscoveredWorkload
from app.core.lookup import at_most_one
from app.core.filters import ref_ids


ADOPTION_CONSTRAINT = "uq_demand_workload_adoption_identity"

DISMISSED = "dismissed"


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

        Rows are read by name: `DiscoveredWorkload`, `WorkloadAdoption`, `cluster_uid`,
        `cluster_name`, `project_uid` and `deployment_uid`.
        """
        return (
            select(
                DiscoveredWorkload,
                Cluster.uid.label("cluster_uid"),
                Cluster.name.label("cluster_name"),
                WorkloadAdoption,
                Project.uid.label("project_uid"),
                Deployment.uid.label("deployment_uid"),
            )
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
        """By id, or by name narrowed by `cluster`. A name seen in two places is refused, never picked."""
        column = DiscoveredWorkload.uid if is_uid(ref) else DiscoveredWorkload.name
        query = self._base().where(column == ref)
        if cluster is not None:
            query = query.where(DiscoveredWorkload.cluster_id.in_(ref_ids(Cluster, cluster)))
        result = await self.db.execute(query.limit(2))
        return at_most_one(list(result.all()), "discovered workload", ref, "use its id or name a cluster")

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
        if status is None:
            # A dismissed sighting is one somebody has already looked at and does not want to
            # see again. The row stays — ingestion owns it and goes on refreshing it — and the
            # list is where the decision is honoured, so `?status=dismissed` still shows them.
            query = query.where((WorkloadAdoption.id.is_(None)) | (WorkloadAdoption.decision != DISMISSED))
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
        result = await self.db.execute(
            ref_ids(Project, ref, "slug").with_only_columns(Project.id, Project.uid).limit(2)
        )
        return at_most_one(list(result.all()), "project", ref)

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

    async def adoption(self, uid: str) -> WorkloadAdoption | None:
        """The decision recorded about a workload uid, sighting or no sighting."""
        result = await self.db.execute(select(WorkloadAdoption).where(WorkloadAdoption.workload_uid == uid).limit(1))
        return result.scalar_one_or_none()

    async def undecide(self, uid: str) -> None:
        await self.db.execute(delete(WorkloadAdoption).where(WorkloadAdoption.workload_uid == uid))
        await self.db.flush()

    async def artifact_by_ref(self, ref: str) -> Artifact | None:
        """The weights a promotion says it is adopting, by id or by name."""
        column = Artifact.uid if is_uid(ref) else Artifact.name
        result = await self.db.execute(select(Artifact).where(column == ref).limit(2))
        return at_most_one(list(result.scalars()), "artifact", ref)

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
        result = await self.db.execute(select(Project).where(column == ref).limit(2))
        return at_most_one(list(result.scalars()), "project", ref)

    async def device_class_of(self, node_uids: Sequence[str]) -> str | None:
        """The class of accelerator the workload is actually running on.

        Read from the devices of the nodes it holds rather than from anything the
        workload says about itself: a workload's `device_class` decides where the
        thing can be placed, and the cluster is the only authority on what its own
        hardware is. Ambiguity resolves to nothing — a workload spanning two
        classes is a question for a person, not a field to guess at.
        """
        if not node_uids:
            return None
        result = await self.db.execute(
            select(Device.device_class)
            .join(Node, Device.node_id == Node.id)
            .where(Node.uid.in_(node_uids), Device.device_class.is_not(None))
            .distinct()
        )
        classes = [row[0] for row in result.all()]
        return classes[0] if len(classes) == 1 else None

    async def artifact_by_reference(self, digest: str | None, uri: str | None) -> str | None:
        """The catalogue's id for weights a sighting names, if it already holds them.

        Matched on the digest first and the source uri second, because the same
        repository registered by two people is one artifact with two spellings of
        its reference — `hf:org/model` and `hf:org/model@main` — while the `hf://`
        location is what both of them fetch.
        """
        if digest:
            found = await self.db.scalar(select(Artifact.uid).where(Artifact.digest == digest))
            if found:
                return found
        if uri:
            found = await self.db.scalar(
                select(Artifact.uid)
                .join(ArtifactLocation, ArtifactLocation.artifact_id == Artifact.id)
                .where(ArtifactLocation.uri == uri)
                .limit(1)
            )
            if found:
                return found
        return None
