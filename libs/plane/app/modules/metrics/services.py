"""Turning a Hangar object into a scope, and a backend's numbers into a response.

The service resolves objects, decides which cluster answers and how strong the
join is, and never renders a query. Which store, and in which dialect, is
`backend.for_source`.

The three scoping joins, and how much each can be trusted:

* **node** — the exporter labels every series with the machine's `hostname`, and
  the plane stores `Node.name`. On Kubernetes these are the same string. Solid.
* **device** — labelled with `UUID`, against `Device.device_uuid`. This is an
  identity the hardware itself issues, so it is the strongest join in the module
  and the reason per-card panels are trustworthy.
* **deployment** — two joins, strongest first.

  Dynamo stamps every vLLM series with `dynamo_namespace`, which identifies the
  serving process rather than the model it serves, and the labels it is composed
  from ride along on the deployment. When they are there the join is exact, and
  two deployments of one model no longer read as one.

  When they are not — a deployment the plane declared, whose worker hash Dynamo
  has not minted yet — it falls back to `model_name`, which is the served model
  and not the deployment. `Recipe.engine_args["served_model_name"]` names it
  first, then `Recipe.name`, and only then the artifact, which holds the
  repository a model is fetched from — `google/gemma-4-31B-it` against a
  `model_name` of `gemma-4-31b-it` — and so selects nothing on its own.

  Both are reported to the client as scope `deployment`; which one answered is
  visible in the selector, not the schema.

Every one of them is narrowed by cluster first. The fleet remote-writes into one
store holding four clusters, two of which are databases, so an unscoped query
does not answer for a cluster — it answers for all of them at once.

A workload is the one object that spans clusters, and it is not a fourth join: it
is the device join and the deployment join, run once per cluster it landed in and
merged afterwards. What can be merged is what `Metric.aggregate` says adds; a
percentile is not, and is left out of the merged figure rather than averaged into
one — see `_merge`.

The fleet is the same widening in the other direction: every cluster one store
holds, in one `cluster` matcher the store aggregates over itself. It is not a
fan-out that this process merges, because merging is where percentiles stop being
measurements — see `fleet`, which refuses rather than approximates.

An endpoint is the one object no cluster can be asked about. It never syncs to
one, and the requests that make its availability number interesting are the ones
that never reached a cluster at all, so it is answered from the gateway's own
series — a fifth join, on `hangar_endpoint`, against a store that has no cluster
label to narrow by.
"""

import logging

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Device
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Allocation
from app.db.models import Deployment
from app.db.models import AllocationDevice
from app.core.filters import ref_ids
from app.config.settings import settings
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound

from .client import MetricsUnavailable
from .promql import is_quantile
from .promql import worker_identity
from .backend import Scope
from .backend import Window
from .backend import Request
from .backend import for_source
from .schemas import SampleSchema
from .schemas import SeriesSchema
from .schemas import SourceSchema
from .schemas import MetricsSchema
from .schemas import BreakdownSchema
from .schemas import SeriesPointSchema
from .sources import Source
from .sources import shared
from .sources import resolve
from .sources import configured
from .registry import BY_NAME
from .registry import ENGINES
from .registry import Metric
from .registry import family


logger = logging.getLogger(__name__)

GATEWAY = "gateway"
"""The source key the gateway's own series are read under. Not a cluster name."""

NO_GATEWAY = (
    "no metrics store for the plane to read the gateway's own series from; set HANGAR_METRICS_SOURCES"
)

FLEET = "fleet"
"""The scope, the ref, and the source name for an answer about every cluster at once."""

NO_FLEET = "no cluster in the fleet has a metrics store to read; check GET /v1/metrics/sources"

FLEET_METRICS: tuple[Metric, ...] = tuple(
    BY_NAME[name]
    for name in (
        "inference.requests_finished",
        "inference.ttft",
        "inference.ttft_p50",
        "inference.requests_running",
        "inference.output_tokens_per_second",
    )
)
"""What a fleet-wide question is worth asking about: traffic, its tail, and throughput.

Not `family("inference")`. A fleet answer is one query per metric over every
cluster's series at once, and the whole vocabulary would be a page of them for
figures nobody put on an overview.
"""

RESERVED_ENDPOINTS: dict[str, str] = {
    "_catalog": "Model catalogue",
    "_unknown": "Unrecognised model",
}
"""What the gateway's two non-endpoint keys are called in a breakdown.

Both are labelled and neither is hidden: `_unknown` growing is a rename somebody
made without telling the callers, and a row that is filtered out cannot show that.
"""


class MetricsService:
    """Reads measurement. Owns no state and writes nothing, in any mode."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.window = settings.METRICS_WINDOW
        self.timeout = settings.METRICS_TIMEOUT

    async def sources(self) -> list[SourceSchema]:
        """Every cluster that can be measured at all, and the store that answers for it.

        Most clusters are answered by the shared store narrowed to their own
        `metrics_label`, so this has to walk the registry rather than read the
        environment. Env entries that match no cluster are listed after the rows:
        a typo in one is exactly the thing this endpoint is checked for.
        """
        rows = (await self.db.execute(select(Cluster).order_by(Cluster.seq.asc()))).scalars().all()
        unmatched = configured()
        items = []
        for cluster in rows:
            unmatched.pop(cluster.name, None)
            unmatched.pop(cluster.uid, None)
            source = resolve(cluster)
            if source is not None:
                items.append(SourceSchema(**source.public()))
        return items + [SourceSchema(**s.public()) for s in unmatched.values()]

    async def _cluster(self, cluster_id: UUID) -> Cluster:
        row = await self.db.execute(select(Cluster).where(Cluster.id == cluster_id))
        cluster = row.scalar_one_or_none()
        if cluster is None:
            raise NotFound("no cluster for this object")
        return cluster

    def _source(self, cluster: Cluster) -> Source | None:
        return resolve(cluster)

    async def _answer(
        self,
        cluster: Cluster,
        scope: Scope,
        ref: str,
        metrics: tuple[Metric, ...],
        window: str | None,
        step: str | None,
        span: int,
        labels: dict[str, str] | None = None,
        with_series: bool = False,
    ) -> MetricsSchema:
        return await self._answer_source(
            self._source(cluster),
            cluster.uid,
            scope,
            ref,
            metrics,
            window,
            step,
            span,
            labels=labels,
            with_series=with_series,
            no_source=_no_source(cluster),
        )

    async def _answer_source(
        self,
        source: Source | None,
        cluster_uid: str | None,
        scope: Scope,
        ref: str,
        metrics: tuple[Metric, ...],
        window: str | None,
        step: str | None,
        span: int,
        labels: dict[str, str] | None = None,
        with_series: bool = False,
        no_source: str = "",
    ) -> MetricsSchema:
        """One answer from one store, for a scope that may belong to no cluster.

        A workload spans clusters, a fleet answer spans all of them and an
        endpoint is served by the gateway rather than by any cluster, so the
        store and the cluster the answer is stamped with are two separate
        arguments here. `no_source` is the caller's own reason, because why a
        store is missing depends on what was being asked for.
        """
        lookback = window or self.window
        answer = MetricsSchema(
            scope=scope.kind,
            ref=ref,
            cluster=cluster_uid,
            source=SourceSchema(**source.public()) if source else None,
            window=lookback,
        )
        if source is None:
            answer.unavailable = no_source or None
            return answer
        if not metrics:
            return answer
        request = Request(
            scope=scope,
            metrics=metrics,
            window=Window(lookback=lookback, step=step or settings.METRICS_STEP, span=span),
        )
        titles = {m.name: m for m in metrics}
        try:
            backend = for_source(source, self.timeout)
            answer.samples = _samples(await backend.samples(request), titles)
            if scope.breakdown:
                answer.breakdown = [
                    BreakdownSchema(key=key, label=(labels or {}).get(key, key), samples=_samples(values, titles))
                    for key, values in sorted((await backend.breakdown(request)).items())
                ]
            if with_series:
                answer.series = [
                    SeriesSchema(
                        metric=curve.metric,
                        title=titles[curve.metric].title,
                        unit=titles[curve.metric].unit,
                        labels=curve.labels,
                        points=[SeriesPointSchema(at=at, value=value) for at, value in curve.points],
                    )
                    for curve in await backend.curves(request)
                ]
        except MetricsUnavailable as error:
            answer.unavailable = error.message
        return answer

    def _scope(
        self,
        kind: str,
        source: Source | None,
        breakdown: str = "",
        **match: tuple[str, ...],
    ) -> Scope:
        """One scope, always carrying the cluster label its source is narrowed by."""
        return Scope(
            kind=kind,
            cluster=source.label if source else "",
            match={k: v for k, v in match.items() if v},
            breakdown=breakdown,
        )

    async def cluster(
        self,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        query = select(Cluster).where(Cluster.uid == ref if is_uid(ref) else Cluster.name == ref)
        cluster = (await self.db.execute(query)).scalar_one_or_none()
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        nodes = (await self.db.execute(select(Node.name).where(Node.cluster_id == cluster.id))).scalars().all()
        scope = self._scope("cluster", self._source(cluster), breakdown="node")
        return await self._answer(
            cluster,
            scope,
            cluster.uid,
            family("gpu"),
            window,
            step,
            span,
            labels={name: name for name in nodes},
            with_series=series,
        )

    async def node(
        self,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        query = select(Node).where(Node.uid == ref if is_uid(ref) else Node.name == ref)
        node = (await self.db.execute(query)).scalars().first()
        if node is None:
            raise NotFound(f"no node {ref!r}")
        cluster = await self._cluster(node.cluster_id)
        devices = (
            await self.db.execute(select(Device.device_uuid, Device.device_index).where(Device.node_id == node.id))
        ).all()
        scope = self._scope("node", self._source(cluster), node=(node.name,), breakdown="device")
        return await self._answer(
            cluster,
            scope,
            node.uid,
            family("gpu"),
            window,
            step,
            span,
            labels={uuid: f"GPU {index}" for uuid, index in devices},
            with_series=series,
        )

    async def pool(
        self,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        query = select(Pool).where(Pool.uid == ref if is_uid(ref) else Pool.name == ref)
        pool = (await self.db.execute(query)).scalars().first()
        if pool is None:
            raise NotFound(f"no pool {ref!r}")
        cluster = await self._cluster(pool.cluster_id)
        nodes = (await self.db.execute(select(Node.name).where(Node.pool_id == pool.id))).scalars().all()
        source = self._source(cluster)
        if not nodes:
            answer = await self._answer(cluster, self._scope("pool", source), pool.uid, (), window, step, span)
            answer.unavailable = "this pool has no nodes"
            return answer
        scope = self._scope("pool", source, node=tuple(nodes), breakdown="node")
        return await self._answer(
            cluster,
            scope,
            pool.uid,
            family("gpu"),
            window,
            step,
            span,
            labels={name: name for name in nodes},
            with_series=series,
        )

    async def device_class(
        self,
        ref: str,
        name: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        """One class of card inside one cluster, broken down by node.

        The class is a hangar name and the exporter has never heard of it. The
        dialect expands it into the vendor strings `libs/packages/contract/devices.json` maps
        onto it; the service only says which class it means.
        """
        query = select(Cluster).where(Cluster.uid == ref if is_uid(ref) else Cluster.name == ref)
        cluster = (await self.db.execute(query)).scalar_one_or_none()
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        nodes = (
            (
                await self.db.execute(
                    select(Node.name)
                    .join(Device, Device.node_id == Node.id)
                    .where(Node.cluster_id == cluster.id, Device.device_class == name)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        scope = self._scope("device_class", self._source(cluster), device_class=(name,), breakdown="node")
        return await self._answer(
            cluster,
            scope,
            name,
            family("gpu"),
            window,
            step,
            span,
            labels={node: node for node in nodes},
            with_series=series,
        )

    async def device(
        self,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        query = select(Device).where(Device.uid == ref if is_uid(ref) else Device.device_uuid == ref)
        device = (await self.db.execute(query)).scalars().first()
        if device is None:
            raise NotFound(f"no device {ref!r}")
        cluster = await self._cluster(device.cluster_id)
        scope = self._scope("device", self._source(cluster), device=(device.device_uuid,))
        return await self._answer(cluster, scope, device.uid, family("gpu"), window, step, span, with_series=series)

    async def deployment(
        self,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        query = (
            select(Deployment, Workload, Recipe, Artifact)
            .join(Workload, Deployment.workload_id == Workload.id)
            .join(Recipe, Workload.recipe_id == Recipe.id)
            .join(Artifact, Recipe.artifact_id == Artifact.id)
            .where(Deployment.uid == ref if is_uid(ref) else Deployment.name == ref)
        )
        row = (await self.db.execute(query)).first()
        if row is None:
            raise NotFound(f"no deployment {ref!r}")
        deployment, workload, recipe, artifact = row
        cluster = await self._cluster(deployment.cluster_id)
        served = served_model(recipe, artifact)
        engine = ENGINES.get(recipe.engine)
        source = self._source(cluster)
        if engine is None or not engine.metrics:
            answer = await self._answer(
                cluster, self._scope("deployment", source), deployment.uid, (), window, step, span
            )
            answer.unavailable = f"no metric dialect registered for engine {recipe.engine!r}"
            return answer
        worker = worker_identity(workload.labels or {})
        if worker:
            scope = self._scope("deployment", source, worker=(worker,))
        else:
            scope = self._scope("deployment", source, deployment=(served,))
        return await self._answer(
            cluster, scope, deployment.uid, engine.metrics, window, step, span, with_series=series
        )

    async def _workload(self, ref: str, project: str | None):
        """The workload a reference names, with the recipe and artifact behind it.

        A name is unique only within its project, so a bare one can name several
        workloads; that is a 400 listing the projects it could have meant rather
        than a guess, exactly as `GET /v1/workloads/{ref}` answers it.
        """
        query = (
            select(Workload, Recipe, Artifact)
            .join(Recipe, Workload.recipe_id == Recipe.id)
            .join(Artifact, Recipe.artifact_id == Artifact.id)
            .where(Workload.uid == ref if is_uid(ref) else Workload.name == ref)
        )
        if project is not None:
            query = query.where(Workload.project_id.in_(ref_ids(Project, project, "slug")))
        found = (await self.db.execute(query.limit(2))).all()
        if not found:
            raise NotFound(f"no workload {ref!r}")
        if len(found) > 1:
            rows = await self.db.execute(
                select(Workload.uid, Project.slug)
                .join(Project, Workload.project_id == Project.id)
                .where(Workload.name == ref)
            )
            raise Invalid(
                f"{ref!r} names a workload in more than one project; say which with `project`",
                {"candidates": [{"id": uid, "project": slug} for uid, slug in rows.all()]},
            )
        return found[0]

    async def _held_devices(self, workload_id: UUID) -> dict[UUID, tuple[str, ...]]:
        """The device UUIDs a workload holds right now, per cluster.

        Live on both halves — the allocation and the membership — because this is
        what the workload occupies now, not what it has occupied. A cluster with
        nothing live is absent from the mapping rather than present with an empty
        tuple: an empty device matcher selects the whole fleet.
        """
        rows = (
            await self.db.execute(
                select(Allocation.cluster_id, Device.device_uuid)
                .select_from(Allocation)
                .join(AllocationDevice, AllocationDevice.allocation_id == Allocation.id)
                .join(Device, AllocationDevice.device_id == Device.id)
                .where(
                    Allocation.workload_id == workload_id,
                    Allocation.released_at.is_(None),
                    AllocationDevice.released_at.is_(None),
                )
            )
        ).all()
        held: dict[UUID, set[str]] = {}
        for cluster_id, device_uuid in rows:
            if device_uuid:
                held.setdefault(cluster_id, set()).add(device_uuid)
        return {cluster_id: tuple(sorted(uuids)) for cluster_id, uuids in held.items()}

    async def workload(
        self,
        ref: str,
        project: str | None = None,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        """One workload, measured in every cluster it landed in and merged back.

        A workload is the object a person asked for and the only one that spans
        clusters, so its answer belongs to no single cluster and says so with
        `cluster: null`. Each cluster is asked twice, because the two families
        join differently: the cards are selected by the UUIDs the workload's live
        allocations hold, and the serving process by the same worker-then-model
        rule `deployment` uses. Every cluster's own numbers survive in the
        breakdown, so a merge that cannot be made honestly costs nothing.

        `source` names the store that answered first, because a workload whose
        clusters sit in different stores has no one store either; which cluster
        each figure came from is in the breakdown.
        """
        workload, recipe, artifact = await self._workload(ref, project)
        lookback = window or self.window
        answer = MetricsSchema(scope="workload", ref=workload.uid, cluster=None, source=None, window=lookback)
        engine = ENGINES.get(recipe.engine)
        if engine is None or not engine.metrics:
            answer.unavailable = f"no metric dialect registered for engine {recipe.engine!r}"
            return answer
        clusters = (
            (
                await self.db.execute(
                    select(Cluster)
                    .join(Deployment, Deployment.cluster_id == Cluster.id)
                    .where(Deployment.workload_id == workload.id)
                    .distinct()
                    .order_by(Cluster.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        if not clusters:
            answer.unavailable = "this workload is not deployed to any cluster"
            return answer

        held = await self._held_devices(workload.id)
        worker = worker_identity(workload.labels or {})
        served = served_model(recipe, artifact)
        metrics = family("gpu") + engine.metrics
        titles = {m.name: m for m in metrics}
        per_cluster: list[tuple[Cluster, dict[str, float | None]]] = []
        reasons: list[str] = []
        failures: list[str] = []

        for cluster in clusters:
            source = self._source(cluster)
            if source is None:
                reasons.append(_no_source(cluster))
                continue
            if answer.source is None:
                answer.source = SourceSchema(**source.public())
            values: dict[str, float | None] = {}
            devices = held.get(cluster.id, ())
            if worker:
                serving = self._scope("workload", source, worker=(worker,))
            else:
                serving = self._scope("workload", source, deployment=(served,))
            asked = [(serving, engine.metrics)]
            if devices:
                asked.insert(0, (self._scope("workload", source, device=devices), family("gpu")))
            for scope, wanted in asked:
                part = await self._answer_source(
                    source,
                    cluster.uid,
                    scope,
                    workload.uid,
                    wanted,
                    window,
                    step,
                    span,
                    with_series=series,
                )
                values.update({sample.metric: sample.value for sample in part.samples})
                answer.series.extend(part.series)
                if part.unavailable:
                    failures.append(part.unavailable)
            per_cluster.append((cluster, values))

        if not per_cluster:
            answer.unavailable = "; ".join(dict.fromkeys(reasons))
            return answer
        answer.samples = _samples(_merge(metrics, [values for _, values in per_cluster]), titles)
        answer.breakdown = [
            BreakdownSchema(key=cluster.uid, label=cluster.name, samples=_samples(values, titles))
            for cluster, values in per_cluster
        ]
        if failures:
            answer.unavailable = failures[0]
        return answer

    async def fleet(
        self,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        """Every measurable cluster at once, as one query the store answers itself.

        The clusters are widened into a single `cluster=~"a|b"` matcher rather than
        asked one at a time and added up here. The plane is not the aggregation
        layer, and the arithmetic is the reason: a rate and a histogram's buckets
        add inside one store, and two p95s from two stores have no mean at all, so
        a merged tail would be a number nothing measured.

        That only holds while one store holds them all, so the refusals are the
        substance of this method. Sources are grouped by the endpoint they point
        at, and anything other than one group of labelled clusters answers with a
        reason and no samples — a store nothing narrows would blend the fleet with
        the clusters beside it, and two stores are two questions.
        """
        rows = (await self.db.execute(select(Cluster).order_by(Cluster.seq.asc()))).scalars().all()
        answer = MetricsSchema(scope=FLEET, ref=FLEET, cluster=None, source=None, window=window or self.window)
        measurable = [(row, source) for row in rows if (source := self._source(row)) is not None]
        if not measurable:
            answer.unavailable = NO_FLEET
            return answer
        groups: dict[tuple[str, str, str], list[tuple[Cluster, Source]]] = {}
        for cluster, source in measurable:
            groups.setdefault((source.url, source.kind, source.token), []).append((cluster, source))
        if len(groups) > 1:
            answer.unavailable = f"the fleet spans {len(groups)} metric stores; ask each cluster"
            return answer
        (url, kind, token), members = next(iter(groups.items()))
        unnarrowed = [cluster for cluster, source in members if not source.label]
        if unnarrowed:
            answer.unavailable = (
                f"cluster {unnarrowed[0].name!r} is answered by a store that carries no cluster label, "
                "so it cannot be widened into a fleet query; ask each cluster"
            )
            return answer
        scope = Scope(
            kind=FLEET,
            cluster="",
            match={"cluster": tuple(sorted(source.label for _, source in members))},
            breakdown="cluster",
        )
        return await self._answer_source(
            Source(cluster=FLEET, url=url, kind=kind, token=token),
            None,
            scope,
            FLEET,
            FLEET_METRICS,
            window,
            step,
            span,
            labels={source.label: cluster.name for cluster, source in members},
            with_series=series,
        )

    def _gateway(self) -> Source | None:
        """The store the gateway's own series are read out of, or nothing.

        The gateway belongs to no cluster, so this does not go through `resolve`:
        it is the shared store under a label that is normally empty, because
        `hangar_gateway_*` comes from one process rather than from a fleet.
        """
        return shared(GATEWAY, settings.METRICS_GATEWAY_LABEL)

    async def endpoint(
        self,
        ref: str,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        """One endpoint as the front door saw it, which is the only place it is visible.

        An endpoint is a plane-only object — it never reaches a cluster — so no
        cluster can be asked about one, and the answer carries `cluster: null`.
        The join is exact: `hangar_endpoint` is the uid, stamped by the hop that
        resolved it.

        A configured store that nobody has pointed at the gateway's metrics port
        answers every one of these with no series, which arrives as an absent
        sample rather than a zero. An endpoint nobody has called is not 100%
        available and is not 0% available either.
        """
        query = select(Endpoint).where(Endpoint.uid == ref if is_uid(ref) else Endpoint.name == ref)
        endpoint = (await self.db.execute(query)).scalars().first()
        if endpoint is None:
            raise NotFound(f"no endpoint {ref!r}")
        source = self._gateway()
        return await self._answer_source(
            source,
            None,
            self._scope("endpoint", source, endpoint=(endpoint.uid,)),
            endpoint.uid,
            family("serving"),
            window,
            step,
            span,
            with_series=series,
            no_source=NO_GATEWAY,
        )

    async def serving(
        self,
        window: str | None = None,
        step: str | None = None,
        span: int = 3600,
        series: bool = False,
    ) -> MetricsSchema:
        """Every request the front door answered, with one breakdown row per endpoint.

        Narrowed by nothing, deliberately: a total assembled by adding up a page
        of endpoints is a total of that page, and it moves when somebody pages.
        The breakdown keeps every endpoint's own figure beside it.

        The gateway's two reserved keys are labelled rather than dropped.
        `_catalog` is `GET /v1/models`, answered from the routing table by no
        endpoint at all, and `_unknown` is a caller naming a model we do not
        serve — which is the traffic worth seeing when a rename has gone wrong.
        """
        source = self._gateway()
        labels = dict(RESERVED_ENDPOINTS)
        if source is not None:
            rows = await self.db.execute(select(Endpoint.uid, Endpoint.name))
            labels.update({uid: name for uid, name in rows.all()})
        return await self._answer_source(
            source,
            None,
            self._scope("serving", source, breakdown="endpoint"),
            "serving",
            family("serving"),
            window,
            step,
            span,
            labels=labels,
            with_series=series,
            no_source=NO_GATEWAY,
        )


def _merge(metrics: tuple[Metric, ...], per_cluster: list[dict[str, float | None]]) -> dict[str, float | None]:
    """Several clusters' numbers for one metric back into one number, or into none.

    `Metric.aggregate` holds across stores for the same reason it holds inside
    one: throughput and power add, a temperature takes the worst card, and a
    utilization averages. A percentile is the exception and is dropped rather
    than merged, because two p95s have no mean and the console reading "not
    measured" is truthful where a number nothing computed is not — the per-cluster
    figures are still in the breakdown, where they are each true of one store.

    A metric every cluster answered with no series stays as `None`, which is the
    difference between an idle workload and one nobody is measuring.
    """
    merged: dict[str, float | None] = {}
    for metric in metrics:
        reported = [values[metric.name] for values in per_cluster if metric.name in values]
        if not reported:
            continue
        numbers = [value for value in reported if value is not None]
        if not numbers:
            merged[metric.name] = None
        elif len(numbers) > 1 and is_quantile(metric.name):
            continue
        elif metric.aggregate == "sum":
            merged[metric.name] = sum(numbers)
        elif metric.aggregate == "max":
            merged[metric.name] = max(numbers)
        else:
            merged[metric.name] = sum(numbers) / len(numbers)
    return merged


def _no_source(cluster: Cluster) -> str:
    """Why a cluster cannot be measured, named as whichever half is missing.

    The two halves fail in different places and are fixed by different people: an
    unlabelled cluster is a row somebody edits through the API, and a missing
    `default=` endpoint is the deployment's configuration.
    """
    if not cluster.metrics_label:
        return (
            f"cluster {cluster.name!r} has no metrics label, so nothing narrows the shared store to it; "
            "set one once the store's `cluster` label shows this cluster"
        )
    return f"no metrics store for the plane to query on behalf of cluster {cluster.name!r}; set HANGAR_METRICS_SOURCES"


def served_model(recipe: Recipe, artifact: Artifact) -> str:
    """The `model_name` the engine reports itself under, most authoritative first.

    An explicit `served_model_name` wins because it is the operator saying so.
    `Recipe.name` comes next: it is the name the deployment is built and served
    under, and it is what the engine emits. The artifact is the last resort
    because it holds the repository a model is fetched from rather than the name
    it serves under — `google/gemma-4-31B-it` against a `model_name` of
    `gemma-4-31b-it` — so reaching it usually means matching no series at all.
    """
    return str(recipe.engine_args.get("served_model_name") or recipe.name or artifact.name)


def _samples(values: dict[str, float | None], metrics: dict[str, Metric]) -> list[SampleSchema]:
    """Numbers back into the response shape, in the order the metrics were asked for."""
    return [
        SampleSchema(
            metric=name,
            title=metrics[name].title,
            unit=metrics[name].unit,
            value=values.get(name),
        )
        for name in metrics
        if name in values
    ]
