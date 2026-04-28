from __future__ import annotations

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import BenchmarkRun
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

    async def artifact_of(self, recipe: Recipe) -> Artifact | None:
        """The artifact a recipe serves, by primary key.

        `_base` already joins the artifact, but only for its uid — an update has to
        re-check the device shape against the weights, and that needs the sizing
        columns rather than the label. Widening the join would put them on every
        list row to serve one write path.
        """
        return await self.db.get(Artifact, recipe.artifact_id)

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

    def workload_usage_query(self, recipe: Recipe) -> Select:
        """Counts the workloads that name this recipe."""
        return select(func.count(Workload.id)).where(Workload.recipe_id == recipe.id)

    async def workload_usage_count(self, recipe: Recipe) -> int:
        result = await self.db.execute(self.workload_usage_query(recipe))
        return int(result.scalar_one())

    def benchmark_runs_query(self, recipe: Recipe) -> Select:
        """The graded, non-discarded benchmark runs recorded against this recipe.

        Only the columns a throughput rollup needs — `summary` is offline
        measurement held in-plane, read through the harness-and-version adapter,
        and a discarded run is a warm-up that must not colour the peak.
        """
        return (
            select(BenchmarkRun.harness, BenchmarkRun.harness_version, BenchmarkRun.summary)
            .where(BenchmarkRun.recipe_id == recipe.id)
            .where(BenchmarkRun.discard == False)
            .order_by(BenchmarkRun.seq.asc())
        )

    async def benchmark_runs(self, recipe: Recipe) -> list[Row]:
        result = await self.db.execute(self.benchmark_runs_query(recipe))
        return list(result.all())

    async def add(self, recipe: Recipe) -> Recipe:
        self.db.add(recipe)
        await self.db.flush()
        return recipe

    async def delete(self, recipe: Recipe) -> None:
        await self.db.delete(recipe)
        await self.db.flush()
