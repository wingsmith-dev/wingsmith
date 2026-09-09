import datetime as dt

from app.core.schemas import Struct


class SourceSchema(Struct):
    cluster: str
    url: str
    kind: str


class SampleSchema(Struct):
    """One measurement at one moment, already reduced to a single number."""

    metric: str
    title: str
    unit: str
    value: float | None
    """`None` means the store had no series, which is not the same as zero."""


class SeriesPointSchema(Struct):
    at: float
    value: float


class SeriesSchema(Struct):
    metric: str
    title: str
    unit: str
    labels: dict[str, str]
    points: list[SeriesPointSchema]


class BreakdownSchema(Struct):
    """One member of a scope — a device under a node, a node under a pool."""

    key: str
    label: str
    samples: list[SampleSchema]


class MetricsSchema(Struct):
    scope: str
    ref: str
    cluster: str | None
    source: SourceSchema | None
    window: str
    samples: list[SampleSchema] = []
    breakdown: list[BreakdownSchema] = []
    series: list[SeriesSchema] = []
    unavailable: str | None = None
    """Why there is nothing here, when there is nothing here."""


class SourceListSchema(Struct):
    items: list[SourceSchema]
    count: int


class PercentilesSchema(Struct):
    """One latency at the three percentiles a contract quotes, in seconds."""

    p50: float | None
    p95: float | None
    p99: float | None


class SlaObjectiveSchema(Struct):
    """The commitment the endpoint was sold under, quoted from its serving quota."""

    success_rate: float | None
    """A fraction in (0, 1]. `None` when the quota commits to no success rate."""
    window_seconds: int
    """The window the commitment's alerting is measured over. The verdict here is over the period."""
    enforced: bool


class SlaReportSchema(Struct):
    """An endpoint's service level over a period, measured at the front door.

    Every figure is the gateway's, over live traffic only, under the availability
    definition named in `definition`. `requests` is every live request in the
    period, `availability` and `failure_ratio` are percentages on 0-100, and the
    latencies are seconds. A figure is `None` when nothing was measured, which is
    not zero; `met` and `error_budget_consumed` are `None` whenever the reading
    or the commitment they need is missing.
    """

    endpoint: str
    endpoint_name: str
    period_start: dt.datetime
    period_end: dt.datetime
    partial: bool
    """The period is still running, so the figures stop at `period_end`, which is now."""
    definition: str
    """The version of the availability definition every figure was computed under."""
    definition_text: str
    source: SourceSchema | None
    requests: int | None
    availability: float | None
    failure_ratio: float | None
    latency: PercentilesSchema
    ttft: PercentilesSchema
    objective: SlaObjectiveSchema | None
    """`None` when the endpoint has no serving quota, so nothing was promised."""
    met: bool | None
    error_budget_consumed: float | None
    """Failure spent over failure allowed: `1.0` is the whole budget, above it is a breach."""
    unavailable: str | None = None
    """Why there are no figures, when there are none."""
