from uuid import UUID

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import or_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Device
from app.db.models import Recipe
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import TopologyDomain
from app.db.models import ArtifactBaseline
from app.db.supply import free_device_sum
from app.core.filters import ref_ids


Base = aliased(Artifact, name="base_artifact")


class ArtifactRepository:
    """Reads artifacts with their locations and the uids of what they point at.

    `base_artifact_id` and `project_id` are foreign keys to rows whose public
    identifier is a uid, so both are resolved in the same statement rather than
    by a follow-up load per artifact.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(Artifact, Base.uid.label("base_uid"), Project.uid.label("project_uid"))
            .outerjoin(Base, Artifact.base_artifact_id == Base.id)
            .outerjoin(Project, Artifact.project_id == Project.id)
            .options(selectinload(Artifact.locations))
        )

    def get_query(self, ref: str) -> Select:
        """An id or a name, per the contract. Prefixes are the client's problem.

        An artifact name is not unique — two quantizations of one base model are
        routinely named the same — so a name resolves to the oldest match. Only
        the digest and the uid identify an artifact exactly.
        """
        if is_uid(ref):
            return self._base().where(Artifact.uid == ref)
        return self._base().where(Artifact.name == ref).order_by(Artifact.seq.asc())

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        kind: str | None = None,
        format: str | None = None,
        project: str | None = None,
        base_artifact: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Select:
        query = self._base().order_by(Artifact.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Artifact.seq > after_seq)
        if kind is not None:
            query = query.where(Artifact.kind == kind)
        if base_artifact is not None:
            query = query.where(Artifact.base_artifact_id.in_(ref_ids(Artifact, base_artifact)))
        if format is not None:
            query = query.where(Artifact.format == format)
        if project is not None:
            query = query.where(Artifact.project_id.in_(ref_ids(Project, project, "slug")))
        if search is not None:
            query = query.where(Artifact.name.ilike(f"%{search}%"))
        for key, value in (labels or {}).items():
            query = query.where(Artifact.metadata_[key].astext == value)
        return query

    async def get(self, ref: str) -> Row | None:
        result = await self.db.execute(self.get_query(ref).limit(1))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.unique().all())

    async def project_by_ref(self, ref: str) -> Project | None:
        column = Project.uid if is_uid(ref) else Project.slug
        result = await self.db.execute(select(Project).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def artifact_by_ref(self, ref: str) -> Artifact | None:
        column = Artifact.uid if is_uid(ref) else Artifact.name
        result = await self.db.execute(select(Artifact).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def artifact_by_digest(self, digest: str) -> Artifact | None:
        """Answers whether the catalogue already holds these exact bytes.

        Separate from `artifact_by_ref` because a digest is not a public ref: the
        contract resolves an artifact by uid or name, and the digest is what a
        resolved source produces before any of those are known.
        """
        result = await self.db.execute(select(Artifact).where(Artifact.digest == digest).limit(1))
        return result.scalar_one_or_none()

    def recipe_usage_query(self, artifact: Artifact) -> Select:
        """Counts the recipes that serve these weights."""
        return select(func.count(Recipe.id)).where(Recipe.artifact_id == artifact.id)

    def workload_usage_query(self, artifact: Artifact) -> Select:
        """Counts the distinct workloads that reach this artifact.

        Either through the recipe's weights or through the workload's own adapter
        set, unioned so a workload naming the artifact both ways counts once.
        """
        return (
            select(func.count(func.distinct(Workload.id)))
            .select_from(Workload)
            .join(Recipe, Workload.recipe_id == Recipe.id)
            .where(
                or_(
                    Recipe.artifact_id == artifact.id,
                    Workload.adapter_artifact_ids.contains([artifact.id]),
                )
            )
        )

    def downstream_query(self, artifact: Artifact) -> Select:
        """Counts the artifacts derived from this one."""
        return select(func.count(Artifact.id)).where(Artifact.base_artifact_id == artifact.id)

    async def usage_counts(self, artifact: Artifact) -> tuple[int, int, int]:
        """The recipe, workload and downstream counts, one round trip each."""
        recipes = await self.db.execute(self.recipe_usage_query(artifact))
        workloads = await self.db.execute(self.workload_usage_query(artifact))
        downstream = await self.db.execute(self.downstream_query(artifact))
        return int(recipes.scalar_one()), int(workloads.scalar_one()), int(downstream.scalar_one())

    def baseline_list_query(
        self,
        artifact_id: UUID,
        limit: int,
        after_seq: int | None = None,
        dataset: str | None = None,
        metric: str | None = None,
    ) -> Select:
        query = (
            select(ArtifactBaseline)
            .where(ArtifactBaseline.artifact_id == artifact_id)
            .order_by(ArtifactBaseline.seq.asc())
            .limit(limit + 1)
        )
        if after_seq is not None:
            query = query.where(ArtifactBaseline.seq > after_seq)
        if dataset is not None:
            query = query.where(ArtifactBaseline.dataset == dataset)
        if metric is not None:
            query = query.where(ArtifactBaseline.metric == metric)
        return query

    def baseline_query(self, artifact_id: UUID, ref: str) -> Select:
        """Scoped to the artifact, so a baseline of another one is not found here.

        A baseline is addressed by uid only: `dataset` and `metric` identify the
        row too, but as a pair and not as a path segment.
        """
        return select(ArtifactBaseline).where(ArtifactBaseline.artifact_id == artifact_id, ArtifactBaseline.uid == ref)

    async def baseline_list(self, **kwargs) -> "list[ArtifactBaseline]":
        result = await self.db.execute(self.baseline_list_query(**kwargs))
        return list(result.scalars().all())

    async def baseline(self, artifact_id: UUID, ref: str) -> ArtifactBaseline | None:
        result = await self.db.execute(self.baseline_query(artifact_id, ref).limit(1))
        return result.scalar_one_or_none()

    def fleet_classes_query(self) -> Select:
        """Every device class the fleet has actually reported, with how much of it.

        Counts, never a placement verdict. Whether eight free devices of a class sit
        in one topology domain right now is a question for `POST /placement/preview`,
        and answering it here would fold a fact that changes by the minute into one
        that only changes when a card is bought.

        `memory_mib` is the largest figure observed for the class rather than the
        contract table's, because the fleet is entitled to hold hardware the table
        has never heard of — `app.core.devices` slugifies those rather than dropping
        them, and a fit against what the cluster reported beats no fit at all.
        """
        return (
            select(
                Device.device_class.label("device_class"),
                func.max(Device.memory_mib).label("memory_mib"),
                func.count(Device.id).label("devices_total"),
                free_device_sum().label("devices_allocatable"),
                func.count(func.distinct(Device.cluster_id)).label("cluster_count"),
            )
            .group_by(Device.device_class)
            .order_by(Device.device_class)
        )

    async def fleet_classes(self) -> "list[Row]":
        result = await self.db.execute(self.fleet_classes_query())
        return list(result.all())

    def devices_per_node_query(self) -> Select:
        """The largest node each device class has been observed in, in devices.

        A node here is a set of devices sharing a coherent interconnect, which is what
        makes it the unit a replica is laid out in — eight on an HGX board, seventy-two in
        a GB300 NVL72, where the rack is many chassis of silicon and one node to anything
        scheduling on it. `supply_topology_domain.device_count` already carries exactly
        that, so the fleet answers the question and nothing is assumed for what we run.

        The maximum rather than the mode: a class present in both an 8-way chassis and a
        72-way rack can be laid out either way, and the wider option is the one a fit
        should be allowed to reach for.
        """
        return (
            select(
                Device.device_class.label("device_class"),
                func.max(TopologyDomain.device_count).label("devices_per_node"),
            )
            .join(TopologyDomain, Device.topology_domain_id == TopologyDomain.id)
            .group_by(Device.device_class)
        )

    async def devices_per_node(self) -> dict[str, int]:
        result = await self.db.execute(self.devices_per_node_query())
        return {row.device_class: row.devices_per_node for row in result.all() if row.devices_per_node}

    async def add(self, artifact: Artifact) -> Artifact:
        self.db.add(artifact)
        await self.db.flush()
        return artifact

    async def add_baseline(self, baseline: ArtifactBaseline) -> ArtifactBaseline:
        self.db.add(baseline)
        await self.db.flush()
        return baseline

    async def delete(self, artifact: Artifact) -> None:
        await self.db.delete(artifact)
        await self.db.flush()

    async def delete_baseline(self, baseline: ArtifactBaseline) -> None:
        await self.db.delete(baseline)
        await self.db.flush()
