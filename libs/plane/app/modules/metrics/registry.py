"""What Hangar measures, named the Hangar way and nothing else.

`libs/packages/contract/API.md` §5 keeps telemetry out of the hub deliberately: capacity is
state and occupancy is measurement, and the two travel by different roads. This
module is the vocabulary of the measurement road — `gpu.utilization`,
`inference.ttft`, what unit each is in, and how a set of them collapses into one
number for a scope.

There is no PromQL here, and that is the point. A metric used to carry the
expression that answered it, which made the API's vocabulary a Prometheus
vocabulary: an upstream rename was a change to the thing clients name, and a
metric that could only be answered from inventory — an hourly cost per node —
had nowhere to live. The expressions moved to `promql.py`, which is one
backend's dialect keyed by these names. Adding ClickHouse or an OTel store is a
second dialect table and a second `Backend`, not a change to anything here.

`SCOPES` is the other half of the vocabulary: the dimensions a measurement can
be narrowed by. They are Hangar's own words for objects the plane stores, and
each backend decides what it calls them — DCGM's label for a node is not
ClickHouse's column for one.
"""

from typing import Literal
from dataclasses import field
from dataclasses import dataclass


Unit = Literal["percent", "celsius", "watts", "mib", "mhz", "seconds", "per_second", "count"]
Family = Literal["gpu", "inference"]

SCOPES: tuple[str, ...] = ("cluster", "node", "pool", "device", "device_class", "deployment", "worker")
"""The dimensions a measurement can be narrowed by, in Hangar's words.

`pool` is not a dimension a store knows: a pool is measured as the set of nodes
in it, which is why it never reaches a backend under its own name.

`deployment` and `worker` are both ways of asking about one deployment and they
are not the same question. `deployment` selects the served model, so two
deployments of one model answer as one. `worker` selects the serving process the
engine registered itself as, which is exact. The service prefers `worker` and
falls back, because only the second is always knowable.
"""


@dataclass(frozen=True)
class Metric:
    """One measurement, named the Hangar way. Vendor-neutral by construction."""

    name: str
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
    Metric("gpu.utilization", "percent", "gpu", "GPU utilization"),
    Metric("gpu.memory_used", "mib", "gpu", "Framebuffer used", aggregate="sum"),
    Metric("gpu.memory_free", "mib", "gpu", "Framebuffer free", aggregate="sum"),
    Metric("gpu.memory_utilization", "percent", "gpu", "Memory bandwidth"),
    Metric("gpu.temperature", "celsius", "gpu", "GPU temperature", aggregate="max"),
    Metric("gpu.memory_temperature", "celsius", "gpu", "Memory temperature", aggregate="max"),
    Metric("gpu.power", "watts", "gpu", "Power draw", aggregate="sum"),
    Metric("gpu.sm_clock", "mhz", "gpu", "SM clock"),
    Metric("gpu.throttle_reasons", "count", "gpu", "Throttle reasons", aggregate="max"),
    Metric("gpu.ecc_uncorrectable", "count", "gpu", "Uncorrectable ECC", aggregate="sum"),
    Metric("gpu.pcie_replays", "count", "gpu", "PCIe replays", aggregate="sum"),
)
"""Per-card health and occupancy. DCGM answers all of these today."""

INFERENCE_METRICS: tuple[Metric, ...] = (
    Metric("inference.ttft", "seconds", "inference", "Time to first token (p95)"),
    Metric("inference.tpot", "seconds", "inference", "Time per output token (p95)"),
    Metric("inference.latency", "seconds", "inference", "End-to-end latency (p95)"),
    Metric("inference.output_tokens_per_second", "per_second", "inference", "Output tokens/sec", aggregate="sum"),
    Metric("inference.input_tokens_per_second", "per_second", "inference", "Input tokens/sec", aggregate="sum"),
    Metric("inference.requests_running", "count", "inference", "Requests running", aggregate="sum"),
    Metric("inference.requests_waiting", "count", "inference", "Requests queued", aggregate="sum"),
    Metric("inference.kv_cache_utilization", "percent", "inference", "KV cache used"),
    Metric("inference.prefix_cache_hit_rate", "percent", "inference", "Prefix cache hit rate"),
    Metric("inference.ttft_p50", "seconds", "inference", "Time to first token (p50)"),
    Metric("inference.ttft_p90", "seconds", "inference", "Time to first token (p90)"),
    Metric("inference.queue_time", "seconds", "inference", "Time waiting in queue (p95)"),
    Metric("inference.prefill_time", "seconds", "inference", "Prefill time (p95)"),
    Metric("inference.decode_time", "seconds", "inference", "Decode time (p95)"),
    Metric("inference.output_tokens_per_stream", "per_second", "inference", "Output tokens/sec, one stream"),
    Metric("inference.requests_finished", "per_second", "inference", "Requests completed/sec", aggregate="sum"),
    Metric("inference.preemptions", "per_second", "inference", "Preemptions/sec", aggregate="sum"),
    Metric("inference.prompt_tokens_per_request", "count", "inference", "Prompt tokens per request (p95)"),
    Metric("inference.generation_tokens_per_request", "count", "inference", "Generated tokens per request (p95)"),
)
"""Serving behaviour. vLLM answers these, with Dynamo fronting it.

Two throughput numbers are registered and they are different measurements, not two
spellings of one. `output_tokens_per_second` sums across every replica in scope and
therefore rises with scale; `output_tokens_per_stream` is what a single caller
experiences and is scale-invariant. An acceptance floor of "30 tokens/second" means
the second — thirty aggregate tokens a second on a serving GPU would be a fault —
and reading the wrong one gives a check that passes under every condition.

`ttft_p50` and `ttft_p90` sit beside the p95 `ttft` because they are named
percentiles somebody is held to, not a query dimension. A percentile a caller can
ask for is a name in this vocabulary; that is the cost of keeping PromQL out of it.

There is no failure-rate metric here and its absence is deliberate. A request that
never reached the cluster — refused at the front door, routed nowhere, timed out
upstream — emits nothing an engine exporter can count, so a ratio built from these
series reports a fully-broken cluster as "no traffic" rather than as an outage. The
number an availability claim rests on has to come from the hop that saw the request.
"""


@dataclass(frozen=True)
class Engine:
    """One serving engine: the metrics it can be asked for."""

    kind: str
    metrics: tuple[Metric, ...] = field(default_factory=tuple)


ENGINES: dict[str, Engine] = {
    "dcgm": Engine(kind="dcgm", metrics=GPU_METRICS),
    "vllm": Engine(kind="vllm", metrics=INFERENCE_METRICS),
}

BY_NAME: dict[str, Metric] = {m.name: m for e in ENGINES.values() for m in e.metrics}


def family(name: Family) -> tuple[Metric, ...]:
    return tuple(m for m in BY_NAME.values() if m.family == name)
