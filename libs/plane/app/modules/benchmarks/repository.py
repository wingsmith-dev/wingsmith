from __future__ import annotations

import datetime as dt

from uuid import UUID

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import BENCHMARK_RUN
from app.core.ids import is_uid
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Operation
from app.db.models import Deployment
from app.db.models import BenchmarkRun
from app.core.filters import ref_ids


class BenchmarkRepository:
    """Reads runs joined to enough identity to render them without a second trip.

    The joins are for public ids only. Everything a run says about what it
    measured is on its own row, snapshotted at creation, so nothing here reaches
    for the recipe to describe the run — only to name it.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(
                BenchmarkRun,
                Deployment.uid.label("deployment_uid"),
                Cluster.uid.label("cluster_uid"),
                Operation.uid.label("operation_uid"),
                Recipe.uid.label("recipe_uid"),
            )
            .join(Deployment, BenchmarkRun.deployment_id == Deployment.id)
            .join(Cluster, BenchmarkRun.cluster_id == Cluster.id)
            .outerjoin(Operation, BenchmarkRun.operation_id == Operation.id)
            .outerjoin(Recipe, BenchmarkRun.recipe_id == Recipe.id)
        )

    def get_query(self, ref: str) -> Select:
        """Runs are addressed by id only.

        A run has no name. Naming one would mean naming a measurement, and the
        thing people actually reach for it by is the deployment it was taken
        against, which is a filter on the list.
        """
        return self._base().where(BenchmarkRun.uid == ref)

    def list_query(
        self,
        limit: int,
        before_seq: int | None = None,
        deployment: str | None = None,
        cluster: str | None = None,
        harness: str | None = None,
        phase: str | None = None,
        actor: str | None = None,
        since: dt.datetime | None = None,
    ) -> Select:
        """Newest first, which is the order a benchmark history is read in."""
        query = self._base().order_by(BenchmarkRun.seq.desc()).limit(limit + 1)
        if before_seq is not None:
            query = query.where(BenchmarkRun.seq < before_seq)
        if deployment is not None:
            query = query.where(BenchmarkRun.deployment_id.in_(ref_ids(Deployment, deployment)))
        if cluster is not None:
            query = query.where(BenchmarkRun.cluster_id.in_(ref_ids(Cluster, cluster)))
        if harness is not None:
            query = query.where(BenchmarkRun.harness == harness)
        if phase is not None:
            query = query.where(BenchmarkRun.phase == phase)
        if actor is not None:
            query = query.where(BenchmarkRun.requested_by == actor)
        if since is not None:
            query = query.where(BenchmarkRun.created_at >= since)
        return query

    async def get(self, ref: str) -> Row | None:
        if not is_uid(ref) or not ref.startswith(BENCHMARK_RUN):
            return None
        result = await self.db.execute(self.get_query(ref))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def deployment_targets(self, ref: str, cluster: str | None = None) -> list[Row]:
        """Every deployment a reference names, with the recipe and artifact behind it.

        Plural because a deployment name is unique per cluster and not above it,
        the same way an operation target is. The recipe and artifact come along
        because the served model name is derived from both, and deriving it at
        creation is what lets the run record what it pointed at rather than
        leaving the operator to guess. The artifact's locations come with it
        because one of them is the `hf://` source the engine resolved `--model`
        against, which is the only place the model's repository survives for an
        artifact registered by digest.
        """
        column = Deployment.uid if is_uid(ref) else Deployment.name
        query = (
            select(Deployment, Recipe, Artifact)
            .join(Workload, Deployment.workload_id == Workload.id)
            .join(Recipe, Workload.recipe_id == Recipe.id)
            .join(Artifact, Recipe.artifact_id == Artifact.id)
            .options(selectinload(Artifact.locations))
            .where(column == ref)
        )
        if cluster is not None:
            query = query.where(Deployment.cluster_id.in_(ref_ids(Cluster, cluster)))
        return list((await self.db.execute(query)).all())

    async def operation_row_id(self, uid: str) -> UUID | None:
        result = await self.db.execute(select(Operation.id).where(Operation.uid == uid))
        return result.scalar_one_or_none()

    async def cluster_uid(self, cluster_id: UUID) -> str | None:
        result = await self.db.execute(select(Cluster.uid).where(Cluster.id == cluster_id))
        return result.scalar_one_or_none()

    async def add(self, run: BenchmarkRun) -> BenchmarkRun:
        self.db.add(run)
        await self.db.flush()
        return run
