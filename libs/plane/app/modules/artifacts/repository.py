from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import or_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Recipe
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Workload
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

    async def add(self, artifact: Artifact) -> Artifact:
        self.db.add(artifact)
        await self.db.flush()
        return artifact

    async def delete(self, artifact: Artifact) -> None:
        await self.db.delete(artifact)
        await self.db.flush()
