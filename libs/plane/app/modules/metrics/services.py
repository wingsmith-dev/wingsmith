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
* **deployment** — vLLM labels by `model_name`, which is the served model, not
  the deployment. Two deployments of the same model in one cluster share a
  series set and will read as one. `Recipe.engine_args["served_model_name"]` is
  honoured when present because that is the operator's chance to disambiguate;
  the fallback is the artifact name. This join is the weak one and the schema
  says so by carrying the scope back to the client.

Every one of them is narrowed by cluster first. The fleet remote-writes into one
store holding four clusters, two of which are databases, so an unscoped query
does not answer for a cluster — it answers for all of them at once.
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
from app.db.models import Artifact
from app.db.models import Deployment
from app.config.settings import settings
from app.core.exceptions import NotFound

from .client import MetricsUnavailable
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
from .sources import resolve
from .sources import configured
from .registry import ENGINES
from .registry import Metric
from .registry import family


logger = logging.getLogger(__name__)


class MetricsService:
    """Reads measurement. Owns no state and writes nothing, in any mode."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.window = settings.METRICS_WINDOW
        self.timeout = settings.METRICS_TIMEOUT

    def sources(self) -> list[SourceSchema]:
        return [SourceSchema(**s.public()) for s in configured().values()]

    async def _cluster(self, cluster_id: UUID) -> Cluster:
        row = await self.db.execute(select(Cluster).where(Cluster.id == cluster_id))
        cluster = row.scalar_one_or_none()
        if cluster is None:
            raise NotFound("no cluster for this object")
        return cluster

    def _source(self, cluster: Cluster) -> Source | None:
        return resolve(cluster.name, cluster.uid)

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
        lookback = window or self.window
        source = self._source(cluster)
        answer = MetricsSchema(
            scope=scope.kind,
            ref=ref,
            cluster=cluster.uid,
            source=SourceSchema(**source.public()) if source else None,
            window=lookback,
        )
        if source is None:
            answer.unavailable = (
                f"no metrics source configured for cluster {cluster.name!r}; set HANGAR_METRICS_SOURCES"
            )
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
            select(Deployment, Recipe, Artifact)
            .join(Recipe, Deployment.recipe_id == Recipe.id)
            .join(Artifact, Recipe.artifact_id == Artifact.id)
            .where(Deployment.uid == ref if is_uid(ref) else Deployment.name == ref)
        )
        row = (await self.db.execute(query)).first()
        if row is None:
            raise NotFound(f"no deployment {ref!r}")
        deployment, recipe, artifact = row
        cluster = await self._cluster(deployment.cluster_id)
        served = recipe.engine_args.get("served_model_name") or artifact.name
        engine = ENGINES.get(recipe.engine)
        source = self._source(cluster)
        if engine is None or not engine.metrics:
            answer = await self._answer(
                cluster, self._scope("deployment", source), deployment.uid, (), window, step, span
            )
            answer.unavailable = f"no metric dialect registered for engine {recipe.engine!r}"
            return answer
        scope = self._scope("deployment", source, deployment=(str(served),))
        return await self._answer(
            cluster, scope, deployment.uid, engine.metrics, window, step, span, with_series=series
        )


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
