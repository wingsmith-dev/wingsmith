"""Turning a Hangar object into a query, and a query result into a number.

The three scoping joins, and how much each can be trusted:

* **node** — DCGM labels every series with `Hostname`, and the plane stores
  `Node.name`. On Kubernetes these are the same string. Solid.
* **device** — DCGM labels with `UUID` and the plane stores `Device.device_uuid`.
  This is an identity the hardware itself issues, so it is the strongest join in
  the module and the reason per-card panels are trustworthy.
* **deployment** — vLLM labels by `model_name`, which is the served model, not
  the deployment. Two deployments of the same model in one cluster share a
  series set and will read as one. `Recipe.engine_args["served_model_name"]` is
  honoured when present because that is the operator's chance to disambiguate;
  the fallback is the artifact name. This join is the weak one and the schema
  says so by carrying the selector back to the client.
"""

import math
import time
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

from .client import Client
from .client import MetricsUnavailable
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
from .registry import SCOPE_LABELS
from .registry import Metric
from .registry import family
from .registry import device_class_values


logger = logging.getLogger(__name__)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _selector(label: str, values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return f'{label}="{_escape(values[0])}"'
    return f'{label}=~"{"|".join(_escape(v) for v in values)}"'


def _device_class_selector(name: str) -> str:
    """Selects the series a hangar device class covers.

    DCGM knows nothing about our classes, so this expands one class into the
    vendor `modelName` strings `contract/devices.json` says map onto it. A class
    the table does not carry falls back to matching its own name literally,
    which selects nothing until the table grows — an unscoped selector here
    would silently report the whole fleet as one card.
    """
    values = list(device_class_values(name))
    if not values:
        logger.warning("metrics.device_class_unmapped class=%s", name)
        values = [name]
    return _selector(SCOPE_LABELS["device_class"], values)


def _render(metric: Metric, selector: str, window: str, by: str = "") -> str:
    """Builds the PromQL for one metric under one selector.

    Registry entries come in two shapes and this is where the difference is
    absorbed: an inference metric carries a whole expression with `$SELECTOR`
    and `$WINDOW` placeholders because rates and quantiles cannot be assembled
    generically, while a DCGM entry is a bare series name that still needs its
    aggregation wrapped around it.
    """
    if "$SELECTOR" in metric.expr:
        expr = metric.expr.replace("$SELECTOR", selector).replace("$WINDOW", window)
        return expr
    inner = f"{metric.expr}{{{selector}}}" if selector else metric.expr
    if by:
        return f"{metric.aggregate} by ({by}) ({inner})"
    return f"{metric.aggregate}({inner})"


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

    async def _gather(
        self,
        source: Source,
        metrics: tuple[Metric, ...],
        selector: str,
        window: str,
    ) -> list[SampleSchema]:
        client = Client(source, self.timeout)
        samples: list[SampleSchema] = []
        for metric in metrics:
            result = await client.instant(_render(metric, selector, window))
            value = None
            if result:
                raw = result[0].get("value", [None, None])[1]
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = None
                if value is not None and math.isnan(value):
                    value = None
            samples.append(SampleSchema(metric=metric.name, title=metric.title, unit=metric.unit, value=value))
        return samples

    async def _breakdown(
        self,
        source: Source,
        metrics: tuple[Metric, ...],
        selector: str,
        window: str,
        by: str,
        labels: dict[str, str],
    ) -> list[BreakdownSchema]:
        """Per-member numbers, so one hot card in a healthy pool is still visible."""
        client = Client(source, self.timeout)
        collected: dict[str, dict[str, SampleSchema]] = {}
        for metric in metrics:
            for entry in await client.instant(_render(metric, selector, window, by=by)):
                key = entry.get("metric", {}).get(by, "")
                if not key:
                    continue
                try:
                    value = float(entry.get("value", [None, None])[1])
                except (TypeError, ValueError):
                    value = None
                collected.setdefault(key, {})[metric.name] = SampleSchema(
                    metric=metric.name, title=metric.title, unit=metric.unit, value=value
                )
        return [
            BreakdownSchema(key=key, label=labels.get(key, key), samples=list(samples.values()))
            for key, samples in sorted(collected.items())
        ]

    async def _series(
        self,
        source: Source,
        metrics: tuple[Metric, ...],
        selector: str,
        window: str,
        step: str,
        span: int,
    ) -> list[SeriesSchema]:
        client = Client(source, self.timeout)
        end = time.time()
        out: list[SeriesSchema] = []
        for metric in metrics:
            for entry in await client.range(_render(metric, selector, window), end - span, end, step):
                points = []
                for at, raw in entry.get("values", []):
                    try:
                        points.append(SeriesPointSchema(at=float(at), value=float(raw)))
                    except (TypeError, ValueError):
                        continue
                out.append(
                    SeriesSchema(
                        metric=metric.name,
                        title=metric.title,
                        unit=metric.unit,
                        labels={k: v for k, v in entry.get("metric", {}).items() if k != "__name__"},
                        points=points,
                    )
                )
        return out

    async def _answer(
        self,
        scope: str,
        ref: str,
        cluster: Cluster,
        metrics: tuple[Metric, ...],
        selector: str,
        window: str | None,
        step: str | None,
        span: int,
        by: str = "",
        labels: dict[str, str] | None = None,
        with_series: bool = False,
    ) -> MetricsSchema:
        window = window or self.window
        source = self._source(cluster)
        answer = MetricsSchema(
            scope=scope,
            ref=ref,
            cluster=cluster.uid,
            source=SourceSchema(**source.public()) if source else None,
            window=window,
        )
        if source is None:
            answer.unavailable = (
                f"no metrics source configured for cluster {cluster.name!r}; set HANGAR_METRICS_SOURCES"
            )
            return answer
        try:
            answer.samples = await self._gather(source, metrics, selector, window)
            if by:
                answer.breakdown = await self._breakdown(source, metrics, selector, window, by, labels or {})
            if with_series:
                answer.series = await self._series(
                    source, metrics, selector, window, step or settings.METRICS_STEP, span
                )
        except MetricsUnavailable as error:
            answer.unavailable = error.message
        return answer

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
        return await self._answer(
            "cluster",
            cluster.uid,
            cluster,
            family("gpu"),
            "",
            window,
            step,
            span,
            by=SCOPE_LABELS["node"],
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
        return await self._answer(
            "node",
            node.uid,
            cluster,
            family("gpu"),
            _selector(SCOPE_LABELS["node"], [node.name]),
            window,
            step,
            span,
            by=SCOPE_LABELS["device"],
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
        if not nodes:
            answer = await self._answer("pool", pool.uid, cluster, (), "", window, step, span)
            answer.unavailable = "this pool has no nodes"
            return answer
        return await self._answer(
            "pool",
            pool.uid,
            cluster,
            family("gpu"),
            _selector(SCOPE_LABELS["node"], list(nodes)),
            window,
            step,
            span,
            by=SCOPE_LABELS["node"],
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

        The class is a hangar name and DCGM has never heard of it, so the
        selector is built from the vendor strings the contract table maps onto
        it rather than from the name itself.
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
        return await self._answer(
            "device_class",
            name,
            cluster,
            family("gpu"),
            _device_class_selector(name),
            window,
            step,
            span,
            by=SCOPE_LABELS["node"],
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
        return await self._answer(
            "device",
            device.uid,
            cluster,
            family("gpu"),
            _selector(SCOPE_LABELS["device"], [device.device_uuid]),
            window,
            step,
            span,
            with_series=series,
        )

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
        if engine is None or not engine.metrics:
            answer = await self._answer("deployment", deployment.uid, cluster, (), "", window, step, span)
            answer.unavailable = f"no metric dialect registered for engine {recipe.engine!r}"
            return answer
        return await self._answer(
            "deployment",
            deployment.uid,
            cluster,
            engine.metrics,
            _selector(engine.identity_label, [str(served)]),
            window,
            step,
            span,
            with_series=series,
        )
