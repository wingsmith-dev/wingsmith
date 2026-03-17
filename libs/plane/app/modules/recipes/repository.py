from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Recipe
from app.db.models import Artifact
from app.core.filters import ref_ids


class RecipeRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Recipe, Artifact.uid.label("artifact_uid")).join(Artifact, Recipe.artifact_id == Artifact.id)

    def get_query(self, ref: str) -> Select:
        """An id or a name. A name resolves to the highest version of it.

        `(name, version)` is the unique key, so a bare name names a family rather
        than a row, and the newest member is the only defensible answer.
        """
        if is_uid(ref):
            return self._base().where(Recipe.uid == ref)
        return self._base().where(Recipe.name == ref).order_by(Recipe.version.desc())

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        artifact: str | None = None,
        engine: str | None = None,
        device_class: str | None = None,
        search: str | None = None,
    ) -> Select:
        query = self._base().order_by(Recipe.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Recipe.seq > after_seq)
        if artifact is not None:
            query = query.where(Recipe.artifact_id.in_(ref_ids(Artifact, artifact)))
        if engine is not None:
            query = query.where(Recipe.engine == engine)
        if device_class is not None:
            query = query.where(Recipe.device_class == device_class)
        if search is not None:
            query = query.where(Recipe.name.ilike(f"%{search}%"))
        return query

    async def get(self, ref: str) -> Row | None:
        result = await self.db.execute(self.get_query(ref).limit(1))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def artifact_by_ref(self, ref: str) -> Artifact | None:
        column = Artifact.uid if is_uid(ref) else Artifact.name
        result = await self.db.execute(select(Artifact).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def add(self, recipe: Recipe) -> Recipe:
        self.db.add(recipe)
        await self.db.flush()
        return recipe

    async def delete(self, recipe: Recipe) -> None:
        await self.db.delete(recipe)
        await self.db.flush()
