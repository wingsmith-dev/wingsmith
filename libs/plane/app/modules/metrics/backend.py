"""The seam: answer these metrics, for this scope, over this window.

`client.py` calls itself the seam and is not one: it is a transport, and
swapping it buys a different HTTP library instead of a different store. The
vocabulary that leaked was PromQL: a metric carried its own expression, so
every caller was one `histogram_quantile` away from a Prometheus.

A `Backend` is asked for Hangar metric names against a Hangar `Scope` and hands
back numbers. Nothing in this file names a query language, a series, or a label,
which is what makes a ClickHouse or OTel implementation a new file instead of a
refactor, and what leaves room for a backend that reads an hourly cost per node
out of inventory and never touches a time-series store at all. `answers()` is
how a backend says which metrics are its own, so a scope's metrics can be split
across more than one.

`Source.kind` selects the implementation. It has been the string `prometheus`
since the module was written and was decorative until now.
"""

from typing import Protocol
from typing import runtime_checkable
from dataclasses import field
from dataclasses import dataclass

from .client import MetricsUnavailable
from .sources import Source
from .registry import Metric


@dataclass(frozen=True)
class Scope:
    """Which slice of the fleet a question is about, in Hangar's words.

    `match` maps a dimension from `registry.SCOPES` onto the values it may take;
    an empty mapping means the whole cluster. `cluster` is separate because it is
    never optional against a shared store: a question with no cluster in it
    blends unrelated fleets into one number.
    """

    kind: str
    cluster: str = ""
    match: dict[str, tuple[str, ...]] = field(default_factory=dict)
    breakdown: str = ""


@dataclass(frozen=True)
class Window:
    """How far back to look, and, for curves, how finely and over how long.

    `at` is the moment a sample is taken, as epoch seconds, and `None` is now. A
    report about a period that has ended is a lookback of the period's length
    taken at its end, not a lookback from now that happens to cover it.
    """

    lookback: str
    step: str = ""
    span: int = 0
    at: float | None = None


@dataclass(frozen=True)
class Request:
    scope: Scope
    metrics: tuple[Metric, ...]
    window: Window


@dataclass(frozen=True)
class Curve:
    """One metric over time. Points are `(epoch_seconds, value)`."""

    metric: str
    labels: dict[str, str]
    points: tuple[tuple[float, float], ...]


@runtime_checkable
class Backend(Protocol):
    """Answers Hangar metrics for a Hangar scope. Knows no query language to a caller."""

    def answers(self, metric: Metric) -> bool:
        """Whether this backend can answer this metric at all."""
        ...

    async def samples(self, request: Request) -> dict[str, float | None]:
        """One number per metric name. `None` is "no series", which is not zero."""
        ...

    async def breakdown(self, request: Request) -> dict[str, dict[str, float | None]]:
        """Per-member numbers, keyed by member then metric name."""
        ...

    async def curves(self, request: Request) -> list[Curve]:
        """The same metrics over time, not at an instant."""
        ...


def for_source(source: Source, timeout: float) -> Backend:
    """The backend that speaks this source's dialect.

    An unknown kind is `MetricsUnavailable`, not a crash: a misconfigured source
    is the same class of problem as an unreachable one and reads the same way to
    a client: a reason, and no number pretending to be zero.
    """
    from .prometheus import PrometheusBackend

    if source.kind == "prometheus":
        return PrometheusBackend(source, timeout)
    raise MetricsUnavailable(f"no metrics backend for source kind {source.kind!r}")
