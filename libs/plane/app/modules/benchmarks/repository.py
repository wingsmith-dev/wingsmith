from __future__ import annotations

import datetime as dt

from uuid import UUID

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import BENCHMARK_RUN
from app.core.ids import BENCHMARK_REQUEST
from app.core.ids import is_uid
from app.db.models import Cluster
from app.db.models import Artifact
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Operation
from app.db.models import Deployment
from app.db.models import BenchmarkRun
from app.db.models import EndpointTarget
from app.db.models import ArtifactBaseline
from app.db.models import BenchmarkRequest
from app.core.filters import ref_ids


class BenchmarkRepository:
    """Reads runs joined to enough identity to render them without a second trip.

    The joins are for public ids only. Everything a run says about what it
    measured is on its own row, snapshotted at creation, so nothing here reaches
    for the workload to describe the run — only to name it.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        """Every join is outer, because an endpoint-level run has neither of the two that were not.

        A run against a public name has no deployment and no cluster — that is
        the point of the level — so an inner join on either would make those
        runs invisible to the list they belong in.
        """
        return (
            select(
                BenchmarkRun,
                Deployment.uid.label("deployment_uid"),
                Cluster.uid.label("cluster_uid"),
                Endpoint.uid.label("endpoint_uid"),
                Operation.uid.label("operation_uid"),
                BenchmarkRequest.uid.label("request_uid"),
            )
            .outerjoin(Deployment, BenchmarkRun.deployment_id == Deployment.id)
            .outerjoin(Cluster, BenchmarkRun.cluster_id == Cluster.id)
            .outerjoin(Endpoint, BenchmarkRun.endpoint_id == Endpoint.id)
            .outerjoin(Operation, BenchmarkRun.operation_id == Operation.id)
            .outerjoin(BenchmarkRequest, BenchmarkRun.request_id == BenchmarkRequest.id)
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
        request: str | None = None,
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
        if request is not None:
            query = query.where(BenchmarkRequest.uid == request)
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
        """Every deployment a reference names, with the workload and artifact behind it.

        Plural because a deployment name is unique per cluster and not above it,
        the same way an operation target is. The workload and artifact come along
        because the served model name is derived from both, and deriving it at
        creation is what lets the run record what it pointed at rather than
        leaving the operator to guess. The artifact's locations come with it
        because one of them is the `hf://` source the engine resolved `--model`
        against, which is the only place the model's repository survives for an
        artifact registered by digest.
        """
        column = Deployment.uid if is_uid(ref) else Deployment.name
        query = (
            select(Deployment, Workload, Artifact, Cluster.uid.label("cluster_uid"))
            .join(Workload, Deployment.workload_id == Workload.id)
            .join(Artifact, Workload.artifact_id == Artifact.id)
            .join(Cluster, Deployment.cluster_id == Cluster.id)
            .options(selectinload(Artifact.locations))
            .where(column == ref)
        )
        if cluster is not None:
            query = query.where(Deployment.cluster_id.in_(ref_ids(Cluster, cluster)))
        return list((await self.db.execute(query)).all())

    async def workload_targets(self, ref: str) -> list[Row]:
        """Every deployment a workload has been placed on, with the workload and artifact behind it.

        A workload-level run is one run per placed deployment, and each of them
        snapshots its own deployment's provenance. The serving configuration is
        the workload's and is therefore the same across all of them — which is
        the reason accuracy is a workload-level claim in the first place.
        """
        column = Workload.uid if is_uid(ref) else Workload.name
        query = (
            select(
                Deployment,
                Workload,
                Artifact,
                Cluster.uid.label("cluster_uid"),
                Workload.uid.label("workload_uid"),
            )
            .join(Workload, Deployment.workload_id == Workload.id)
            .join(Artifact, Workload.artifact_id == Artifact.id)
            .join(Cluster, Deployment.cluster_id == Cluster.id)
            .options(selectinload(Artifact.locations))
            .where(column == ref)
            .order_by(Deployment.seq)
        )
        return list((await self.db.execute(query)).all())

    async def endpoint_targets(self, ref: str) -> list[Row]:
        """The endpoint a reference names, and everything currently enabled behind it.

        Plural rows for one endpoint: the endpoint repeats and each row carries
        one upstream. The run itself is singular — a request to the public name
        is routed by weight and measures whatever the gateway chose — so these
        rows become the request's coverage snapshot rather than N runs.
        """
        column = Endpoint.uid if is_uid(ref) else Endpoint.name
        query = (
            select(Endpoint, Deployment, Workload, Artifact, Cluster.uid.label("cluster_uid"))
            .outerjoin(
                EndpointTarget,
                (EndpointTarget.endpoint_id == Endpoint.id) & EndpointTarget.enabled.is_(True),
            )
            .outerjoin(Deployment, EndpointTarget.deployment_id == Deployment.id)
            .outerjoin(Workload, Deployment.workload_id == Workload.id)
            .outerjoin(Artifact, Workload.artifact_id == Artifact.id)
            .outerjoin(Cluster, Deployment.cluster_id == Cluster.id)
            .where(column == ref)
        )
        return list((await self.db.execute(query)).all())

    def facts_query(self, ref: str) -> Select:
        """One run with everything a verdict needs that is not on its own row.

        The artifact's parameter count chooses which output-speed population the
        run is held to, and the request carries the criteria version, the tier
        and the coverage flag. Every join is outer: a run whose deployment has
        since been deleted still grades, on the checks that do not need it.
        """
        return (
            select(
                BenchmarkRun,
                Artifact.id.label("artifact_id"),
                Artifact.parameter_count.label("parameter_count"),
                BenchmarkRequest.level.label("level"),
                BenchmarkRequest.criteria_version.label("criteria_version"),
                BenchmarkRequest.tier.label("tier"),
                BenchmarkRequest.homogeneous.label("homogeneous"),
                BenchmarkRequest.parameters.label("request_parameters"),
            )
            .outerjoin(Deployment, BenchmarkRun.deployment_id == Deployment.id)
            .outerjoin(Workload, Deployment.workload_id == Workload.id)
            .outerjoin(Artifact, Workload.artifact_id == Artifact.id)
            .outerjoin(BenchmarkRequest, BenchmarkRun.request_id == BenchmarkRequest.id)
            .where(BenchmarkRun.uid == ref)
        )

    async def facts(self, ref: str) -> Row | None:
        if not is_uid(ref) or not ref.startswith(BENCHMARK_RUN):
            return None
        return (await self.db.execute(self.facts_query(ref))).first()

    async def baselines(self, artifact_id: UUID | None) -> list[ArtifactBaseline]:
        """The quality floors declared for one artifact, or nothing when none are.

        A dataset with no declared baseline grades `inconclusive`, not empty.
        """
        if artifact_id is None:
            return []
        query = select(ArtifactBaseline).where(ArtifactBaseline.artifact_id == artifact_id)
        return list((await self.db.execute(query)).scalars().all())

    def request_query(self, ref: str) -> Select:
        return (
            select(
                BenchmarkRequest,
                Deployment.uid.label("deployment_uid"),
                Workload.uid.label("workload_uid"),
                Endpoint.uid.label("endpoint_uid"),
            )
            .outerjoin(Deployment, BenchmarkRequest.deployment_id == Deployment.id)
            .outerjoin(Workload, BenchmarkRequest.workload_id == Workload.id)
            .outerjoin(Endpoint, BenchmarkRequest.endpoint_id == Endpoint.id)
            .where(BenchmarkRequest.uid == ref)
        )

    async def request(self, ref: str) -> Row | None:
        if not is_uid(ref) or not ref.startswith(BENCHMARK_REQUEST):
            return None
        return (await self.db.execute(self.request_query(ref))).first()

    def request_runs_query(self, ref: str) -> Select:
        """Every run of one request, in the order the sweep declared them.

        `step_index` and not `seq`: the steps of a ramp are ordered by the load
        they applied, and creation order is only accidentally the same.
        """
        return (
            select(BenchmarkRun)
            .join(BenchmarkRequest, BenchmarkRun.request_id == BenchmarkRequest.id)
            .where(BenchmarkRequest.uid == ref)
            .order_by(BenchmarkRun.step_index, BenchmarkRun.seq)
        )

    async def request_runs(self, ref: str) -> list[BenchmarkRun]:
        return list((await self.db.execute(self.request_runs_query(ref))).scalars().all())

    def serving_runs_query(self, digest: str) -> Select:
        """The graded, non-discarded runs recorded against one serving configuration.

        Only the columns a throughput rollup needs — `summary` is offline
        measurement held in-plane, read through the harness-and-version adapter,
        and a discarded run is a warm-up that must not colour the peak.

        Matched on the digest the run snapshotted, so it needs no join at all and
        keeps answering after the workload that asked for the run has been
        renamed, rescaled or deleted. The recipe rollup this replaces joined a
        mutable row and could not say either of those things.
        """
        return (
            select(BenchmarkRun.harness, BenchmarkRun.harness_version, BenchmarkRun.summary)
            .where(BenchmarkRun.serving_digest == digest)
            .where(BenchmarkRun.discard == False)
            .order_by(BenchmarkRun.seq.asc())
        )

    async def serving_runs(self, digest: str) -> list[Row]:
        return list((await self.db.execute(self.serving_runs_query(digest))).all())

    async def add_request(self, request: BenchmarkRequest) -> BenchmarkRequest:
        self.db.add(request)
        await self.db.flush()
        return request

    async def operation_row_id(self, uid: str) -> UUID | None:
        result = await self.db.execute(select(Operation.id).where(Operation.uid == uid))
        return result.scalar_one_or_none()

    async def cluster_uids(self, cluster_ids: set[UUID]) -> dict[UUID, str]:
        """Each cluster's public id by row id, in one query. A deleted cluster is missing."""
        if not cluster_ids:
            return {}
        result = await self.db.execute(select(Cluster.id, Cluster.uid).where(Cluster.id.in_(cluster_ids)))
        return {row.id: row.uid for row in result}

    async def add(self, run: BenchmarkRun) -> BenchmarkRun:
        self.db.add(run)
        await self.db.flush()
        return run
