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
