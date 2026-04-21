from __future__ import annotations

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Deployment
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
        lifecycle: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
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
        if lifecycle is not None:
            query = query.where(Recipe.lifecycle == lifecycle)
        if search is not None:
            query = query.where(Recipe.name.ilike(f"%{search}%"))
        for key, value in (labels or {}).items():
            query = query.where(Recipe.labels[key].astext == value)
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

    async def clusters_running(self, recipe: Recipe) -> list[str]:
        """The clusters a change to this recipe is worth waking, by uid.

        A recipe is global and a deployment is not, so there is no single cluster
        to announce to — the reachable set is whichever clusters were actually
        told about the recipe, which is the same set `_recipes` renders it into.
        A disabled deployment is not in it, because the cluster was never sent the
        recipe on its behalf either.
        """
        query = (
            select(Cluster.uid)
            .join(Deployment, Deployment.cluster_id == Cluster.id)
            .join(Workload, Deployment.workload_id == Workload.id)
            .where(Workload.recipe_id == recipe.id, Deployment.enabled == True)
            .distinct()
        )
        result = await self.db.execute(query)
        return list(result.scalars())

    async def add(self, recipe: Recipe) -> Recipe:
        self.db.add(recipe)
        await self.db.flush()
        return recipe

    async def delete(self, recipe: Recipe) -> None:
        await self.db.delete(recipe)
        await self.db.flush()
