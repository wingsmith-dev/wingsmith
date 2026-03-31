"""What each metric is called in the engine that emits it.

`contract/API.md` §5 keeps telemetry out of the hub deliberately: capacity is
state and occupancy is measurement, and the two travel by different roads. This
module is the whole of the plane's opinion about the measurement road. Every
query elsewhere names a metric by its Hangar name — `gpu.utilization`,
`inference.ttft` — and this table is the only place that knows DCGM spells it
`DCGM_FI_DEV_GPU_UTIL` and vLLM spells its own
`vllm:time_to_first_token_seconds`.

Being opinionated is the point: today the fleet runs DCGM and vLLM behind
Dynamo, so those are the two dialects here. Adding SGLang or TensorRT-LLM means
adding a dialect to `ENGINES`, not touching a service or a route.

`SCOPE_LABELS` records the other half of the mapping — how a Hangar object is
selected inside a metric source. DCGM tags a series with the machine's hostname
and the GPU's UUID, which are the two identifiers the plane already stores, so
node and device scoping need no correlation table. A deployment is selected by
the model it serves, which is the weakest join of the three and is called out in
`services.py` where it is made.
"""

from typing import Literal
from dataclasses import field
from dataclasses import dataclass

from app.core.devices import DIALECT_DCGM
from app.core.devices import table


Unit = Literal["percent", "celsius", "watts", "mib", "mhz", "seconds", "per_second", "count"]
Family = Literal["gpu", "inference"]


@dataclass(frozen=True)
class Metric:
    """One measurement, named the Hangar way and mapped to one dialect's name."""

    name: str
    expr: str
    unit: Unit
    family: Family
    title: str
    aggregate: Literal["avg", "sum", "max"] = "avg"
    """How a set of series collapses into one number for a scope.

    Utilization averages across a pool; tokens per second sums; temperature takes
    the max, because a pool whose hottest card is at 89°C is not an 62°C pool and
    reporting the mean would hide exactly the card you need to see.
    """


GPU_METRICS: tuple[Metric, ...] = (
    Metric("gpu.utilization", "DCGM_FI_DEV_GPU_UTIL", "percent", "gpu", "GPU utilization"),
    Metric("gpu.memory_used", "DCGM_FI_DEV_FB_USED", "mib", "gpu", "Framebuffer used", aggregate="sum"),
    Metric("gpu.memory_free", "DCGM_FI_DEV_FB_FREE", "mib", "gpu", "Framebuffer free", aggregate="sum"),
    Metric("gpu.memory_utilization", "DCGM_FI_DEV_MEM_COPY_UTIL", "percent", "gpu", "Memory bandwidth"),
    Metric("gpu.temperature", "DCGM_FI_DEV_GPU_TEMP", "celsius", "gpu", "GPU temperature", aggregate="max"),
    Metric(
        "gpu.memory_temperature", "DCGM_FI_DEV_MEMORY_TEMP", "celsius", "gpu", "Memory temperature", aggregate="max"
    ),
    Metric("gpu.power", "DCGM_FI_DEV_POWER_USAGE", "watts", "gpu", "Power draw", aggregate="sum"),
    Metric("gpu.sm_clock", "DCGM_FI_DEV_SM_CLOCK", "mhz", "gpu", "SM clock"),
    Metric(
        "gpu.throttle_reasons",
        "DCGM_FI_DEV_CLOCK_THROTTLE_REASONS",
        "count",
        "gpu",
        "Throttle reasons",
        aggregate="max",
    ),
    Metric(
        "gpu.ecc_uncorrectable", "DCGM_FI_DEV_ECC_DBE_AGG_TOTAL", "count", "gpu", "Uncorrectable ECC", aggregate="sum"
    ),
    Metric("gpu.pcie_replays", "DCGM_FI_DEV_PCIE_REPLAY_COUNTER", "count", "gpu", "PCIe replays", aggregate="sum"),
)
"""DCGM. Names are the exporter's field names and are stable across versions."""

INFERENCE_METRICS: tuple[Metric, ...] = (
    Metric(
        "inference.ttft",
        "histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{$SELECTOR}[$WINDOW])))",
        "seconds",
        "inference",
        "Time to first token (p95)",
    ),
    Metric(
        "inference.tpot",
        "histogram_quantile(0.95, sum by (le) (rate(vllm:time_per_output_token_seconds_bucket{$SELECTOR}[$WINDOW])))",
        "seconds",
        "inference",
        "Time per output token (p95)",
    ),
    Metric(
        "inference.latency",
        "histogram_quantile(0.95, sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket{$SELECTOR}[$WINDOW])))",
        "seconds",
        "inference",
        "End-to-end latency (p95)",
    ),
    Metric(
        "inference.output_tokens_per_second",
        "sum(rate(vllm:generation_tokens_total{$SELECTOR}[$WINDOW]))",
        "per_second",
        "inference",
        "Output tokens/sec",
        aggregate="sum",
    ),
    Metric(
        "inference.input_tokens_per_second",
        "sum(rate(vllm:prompt_tokens_total{$SELECTOR}[$WINDOW]))",
        "per_second",
        "inference",
        "Input tokens/sec",
        aggregate="sum",
    ),
    Metric(
        "inference.requests_running",
        "sum(vllm:num_requests_running{$SELECTOR})",
        "count",
        "inference",
        "Requests running",
        aggregate="sum",
    ),
    Metric(
        "inference.requests_waiting",
        "sum(vllm:num_requests_waiting{$SELECTOR})",
        "count",
        "inference",
        "Requests queued",
        aggregate="sum",
    ),
    Metric(
        "inference.kv_cache_utilization",
        "avg(vllm:gpu_cache_usage_perc{$SELECTOR}) * 100",
        "percent",
        "inference",
        "KV cache used",
    ),
    Metric(
        "inference.prefix_cache_hit_rate",
        "sum(rate(vllm:prefix_cache_hits_total{$SELECTOR}[$WINDOW])) / clamp_min(sum(rate(vllm:prefix_cache_queries_total{$SELECTOR}[$WINDOW])), 1) * 100",
        "percent",
        "inference",
        "Prefix cache hit rate",
    ),
)
"""vLLM. Dynamo fronts these workers but does not rename their series."""


@dataclass(frozen=True)
class Engine:
    """One dialect: the metrics it emits and the label its series are keyed by."""

    kind: str
    metrics: tuple[Metric, ...] = field(default_factory=tuple)
    identity_label: str = ""


ENGINES: dict[str, Engine] = {
    "dcgm": Engine(kind="dcgm", metrics=GPU_METRICS),
    "vllm": Engine(kind="vllm", metrics=INFERENCE_METRICS, identity_label="model_name"),
}

BY_NAME: dict[str, Metric] = {m.name: m for e in ENGINES.values() for m in e.metrics}

SCOPE_LABELS = {
    "node": "Hostname",
    "device": "UUID",
    "device_class": "modelName",
}
"""DCGM's label names for the identifiers the plane already stores.

`device_class` is the one that is not an identity: DCGM puts the vendor's
marketing name in `modelName`, and a hangar class is our name for a set of those.
The translation is `device_class_values`, never the label on its own.
"""


def device_class_values(name: str) -> tuple[str, ...]:
    """The `modelName` values a hangar device class is emitted under.

    Empty when the class is not in `contract/devices.json` — which happens for
    hardware the operator slugified rather than looked up. The caller decides
    what to do with that; it must not turn into an unscoped query.
    """
    return table().aliases(name, DIALECT_DCGM)


def family(name: Family) -> tuple[Metric, ...]:
    return tuple(m for m in BY_NAME.values() if m.family == name)
