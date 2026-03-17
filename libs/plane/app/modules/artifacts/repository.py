from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Project
from app.db.models import Artifact
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
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Select:
        query = self._base().order_by(Artifact.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Artifact.seq > after_seq)
        if kind is not None:
            query = query.where(Artifact.kind == kind)
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

    async def add(self, artifact: Artifact) -> Artifact:
        self.db.add(artifact)
        await self.db.flush()
        return artifact

    async def delete(self, artifact: Artifact) -> None:
        await self.db.delete(artifact)
        await self.db.flush()
