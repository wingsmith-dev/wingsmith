"""The Prometheus `Backend`: Hangar metrics in, numbers out, PromQL in between.

Two things here are worth knowing about.

**Candidate fallback is a top-level `or`.** Each candidate series is rendered
into its own complete, already-aggregated expression and the results are unioned.
Verified against the fleet's store: an absent series alone returns no series,
`absent or present` returns the present one, and both-absent returns a clean
empty instead of an error, which `services` maps to `None` and the console
renders as an em dash.

**A sum never unions.** `or` is a set union, so if two candidates were ever
populated at once, an upgrade window where an engine emits both the old and the
new name, a union under a `sum` would add the same tokens twice. A silently
doubled token rate is the worst failure this module can have, so a summed metric
with more than one candidate probes them in order and uses the first that has
any series at all, instead of trusting the shape of the union. Averages and
maxima are idempotent under the union and take the cheaper path.
"""

import math
import time

from .client import Client
from .promql import LABELS
from .promql import DIALECT
from .promql import render
from .promql import selector
from .backend import Curve
from .backend import Request
from .sources import Source
from .registry import Metric


class PrometheusBackend:
    """Answers `registry` metrics out of one Prometheus-compatible store."""

    def __init__(self, source: Source, timeout: float) -> None:
        self.source = source
        self.client = Client(source, timeout)

    def answers(self, metric: Metric) -> bool:
        return metric.name in DIALECT

    def _scoped(self, request: Request) -> str:
        return selector(request.scope)

    async def _series_names(self, metric: Metric, where: str) -> tuple[str, ...]:
        """The candidates to render, after ruling out a double count.

        A summed metric collapses to exactly one candidate, the first with any
        series under this selector, so the union can never add two spellings of
        the same measurement together. When none of them are populated the last
        is used, so the query is well-formed and simply returns nothing.
        """
        candidates = DIALECT[metric.name].candidates
        if metric.aggregate != "sum" or len(candidates) < 2:
            return candidates
        for candidate in candidates:
            probe = f"count({candidate}{{{where}}})" if where else f"count({candidate})"
            if await self.client.instant(probe):
                return (candidate,)
        return candidates[-1:]

    async def _expression(self, metric: Metric, where: str, window: str, by: str = "") -> str:
        names = await self._series_names(metric, where)
        return " or ".join(render(metric, name, where, window, by) for name in names)

    async def samples(self, request: Request) -> dict[str, float | None]:
        where = self._scoped(request)
        out: dict[str, float | None] = {}
        for metric in request.metrics:
            if not self.answers(metric):
                out[metric.name] = None
                continue
            expression = await self._expression(metric, where, request.window.lookback)
            result = await self._instant(expression, request.window.at)
            out[metric.name] = _value(result[0]) if result else None
        return out

    async def _instant(self, expression: str, at: float | None) -> list[dict]:
        """One instant query, taken now or at the moment the window names."""
        if at is None:
            return await self.client.instant(expression)
        return await self.client.instant(expression, at=at)

    async def breakdown(self, request: Request) -> dict[str, dict[str, float | None]]:
        by = LABELS.get(request.scope.breakdown, "")
        if not by:
            return {}
        where = self._scoped(request)
        collected: dict[str, dict[str, float | None]] = {}
        for metric in request.metrics:
            if not self.answers(metric):
                continue
            expression = await self._expression(metric, where, request.window.lookback, by=by)
            for entry in await self._instant(expression, request.window.at):
                key = entry.get("metric", {}).get(by, "")
                if not key:
                    continue
                collected.setdefault(key, {})[metric.name] = _value(entry)
        return collected

    async def curves(self, request: Request) -> list[Curve]:
        where = self._scoped(request)
        window = request.window
        end = time.time()
        out: list[Curve] = []
        for metric in request.metrics:
            if not self.answers(metric):
                continue
            expression = await self._expression(metric, where, window.lookback)
            for entry in await self.client.range(expression, end - window.span, end, window.step):
                points: list[tuple[float, float]] = []
                for at, raw in entry.get("values", []):
                    try:
                        points.append((float(at), float(raw)))
                    except (TypeError, ValueError):
                        continue
                out.append(
                    Curve(
                        metric=metric.name,
                        labels={k: v for k, v in entry.get("metric", {}).items() if k != "__name__"},
                        points=tuple(points),
                    )
                )
        return out


def _value(entry: dict) -> float | None:
    """One sample as a number, or `None`, which means no series, never zero.

    Prometheus answers a quantile over an empty histogram with `NaN`, and a NaN
    drawn as 0% reads as an idle GPU, which is the one wrong answer that looks
    right.
    """
    try:
        value = float(entry.get("value", [None, None])[1])
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value
