"""One backend's dialect: what Hangar's metrics are called in Prometheus.

Everything Prometheus-shaped lives here — the series names, the label names, the
expression each metric is answered by — keyed by the Hangar name in
`registry.py`. Nothing else in the module renders PromQL.

**Candidates, not a name.** An expression carries its source series in
preference order rather than one hardcoded name, because upstream renames and
we find out by a panel going blank. `vllm:time_per_output_token_seconds` became
`vllm:inter_token_latency_seconds` and `vllm:gpu_cache_usage_perc` became
`vllm:kv_cache_usage_perc`; both are still listed, first, so a cluster that has
not been upgraded keeps answering. Names DCGM does not emit today —
`DCGM_FI_DEV_CLOCK_THROTTLE_REASONS`, the ECC aggregates — stay listed for the
same reason in the other direction: the panel reads "no data" until the
exporter's field csv is extended, and then it works with no code change.

**Labels.** `hostname`, lowercase, is what the deployed dcgm-exporter emits;
`Hostname` matched zero series and emptied every node and pool panel. The
workload's pod is `exported_pod`, never `pod` — `pod` belongs to the exporter.
And `cluster` is on every series because the fleet remote-writes into one store,
so a query without it sums two inference clusters and two database clusters
into one number.
"""

import logging

from dataclasses import dataclass

from app.core.devices import DIALECT_DCGM
from app.core.devices import table

from .backend import Scope
from .registry import Metric


logger = logging.getLogger(__name__)

LABELS: dict[str, str] = {
    "cluster": "cluster",
    "node": "hostname",
    "device": "UUID",
    "device_class": "modelName",
    "deployment": "model_name",
    "worker": "dynamo_namespace",
}
"""The label each Hangar dimension is selected by. Verified against the fleet's store.

`dynamo_namespace` is not a namespace in the Kubernetes sense and is not the
`dynamo-namespace` pod label either. Dynamo composes it at runtime from
`DYN_NAMESPACE` and `DYN_NAMESPACE_WORKER_SUFFIX`, which reach the plane as the
`dynamo-namespace` and `dynamo-worker-hash` labels — see `worker_identity`. It is
the only label on a vLLM series that distinguishes one deployment of a model from
another.
"""


WORKER_NAMESPACE = "dynamo-namespace"
WORKER_HASH = "dynamo-worker-hash"


def worker_identity(labels: dict) -> str:
    """The `dynamo_namespace` a deployment's engine reports itself under, or empty.

    Dynamo joins the graph's namespace to the worker's hash with a hyphen, and
    the result cannot be rebuilt from the deployment's name: three of the fleet's
    four deployments render `dynamo-<hash>` while one whose graph sets its own
    `DYN_NAMESPACE` renders `dynamo-<graph>-<hash>`. Both halves arrive as pod
    labels, so this reads them rather than guessing.

    Empty when either half is missing, which is the honest answer for a
    deployment the plane declared itself: Dynamo mints the hash after the fact,
    so there is nothing to know until it has been observed.
    """
    namespace = str(labels.get(WORKER_NAMESPACE) or "").strip()
    worker = str(labels.get(WORKER_HASH) or "").strip()
    return f"{namespace}-{worker}" if namespace and worker else ""


@dataclass(frozen=True)
class Expression:
    """How one Hangar metric is answered, and out of which series.

    `template` carries `$SERIES`, `$SELECTOR` and `$WINDOW`. An empty template
    means the series is read as-is and only needs the metric's own aggregation
    wrapped around it, which is the whole of DCGM.
    """

    candidates: tuple[str, ...]
    template: str = ""


QUANTILE = "histogram_quantile(0.95, sum by (le) (rate($SERIES{$SELECTOR}[$WINDOW])))"
P90 = "histogram_quantile(0.90, sum by (le) (rate($SERIES{$SELECTOR}[$WINDOW])))"
P50 = "histogram_quantile(0.50, sum by (le) (rate($SERIES{$SELECTOR}[$WINDOW])))"
RATE = "sum(rate($SERIES{$SELECTOR}[$WINDOW]))"

DIALECT: dict[str, Expression] = {
    "gpu.utilization": Expression(("DCGM_FI_DEV_GPU_UTIL",)),
    "gpu.memory_used": Expression(("DCGM_FI_DEV_FB_USED",)),
    "gpu.memory_free": Expression(("DCGM_FI_DEV_FB_FREE",)),
    "gpu.memory_utilization": Expression(("DCGM_FI_DEV_MEM_COPY_UTIL",)),
    "gpu.temperature": Expression(("DCGM_FI_DEV_GPU_TEMP",)),
    "gpu.memory_temperature": Expression(("DCGM_FI_DEV_MEMORY_TEMP",)),
    "gpu.power": Expression(("DCGM_FI_DEV_POWER_USAGE",)),
    "gpu.sm_clock": Expression(("DCGM_FI_DEV_SM_CLOCK",)),
    "gpu.throttle_reasons": Expression(("DCGM_FI_DEV_CLOCK_THROTTLE_REASONS", "DCGM_FI_DEV_CLOCKS_EVENT_REASONS")),
    "gpu.ecc_uncorrectable": Expression(("DCGM_FI_DEV_ECC_DBE_AGG_TOTAL", "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL")),
    "gpu.pcie_replays": Expression(("DCGM_FI_DEV_PCIE_REPLAY_COUNTER",)),
    "inference.ttft": Expression(("vllm:time_to_first_token_seconds_bucket",), QUANTILE),
    "inference.tpot": Expression(
        ("vllm:time_per_output_token_seconds_bucket", "vllm:inter_token_latency_seconds_bucket"),
        QUANTILE,
    ),
    "inference.latency": Expression(("vllm:e2e_request_latency_seconds_bucket",), QUANTILE),
    "inference.output_tokens_per_second": Expression(
        ("vllm:generation_tokens_total",), "sum(rate($SERIES{$SELECTOR}[$WINDOW]))"
    ),
    "inference.input_tokens_per_second": Expression(
        ("vllm:prompt_tokens_total",), "sum(rate($SERIES{$SELECTOR}[$WINDOW]))"
    ),
    "inference.requests_running": Expression(("vllm:num_requests_running",), "sum($SERIES{$SELECTOR})"),
    "inference.requests_waiting": Expression(("vllm:num_requests_waiting",), "sum($SERIES{$SELECTOR})"),
    "inference.kv_cache_utilization": Expression(
        ("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc"), "avg($SERIES{$SELECTOR}) * 100"
    ),
    "inference.prefix_cache_hit_rate": Expression(
        ("vllm:prefix_cache_hits_total",),
        "sum(rate($SERIES{$SELECTOR}[$WINDOW])) / clamp_min(sum(rate(vllm:prefix_cache_queries_total{$SELECTOR}[$WINDOW])), 1) * 100",
    ),
    "inference.ttft_p50": Expression(("vllm:time_to_first_token_seconds_bucket",), P50),
    "inference.ttft_p90": Expression(("vllm:time_to_first_token_seconds_bucket",), P90),
    "inference.queue_time": Expression(("vllm:request_queue_time_seconds_bucket",), QUANTILE),
    "inference.prefill_time": Expression(("vllm:request_prefill_time_seconds_bucket",), QUANTILE),
    "inference.decode_time": Expression(("vllm:request_decode_time_seconds_bucket",), QUANTILE),
    "inference.output_tokens_per_stream": Expression(
        ("vllm:time_per_output_token_seconds_bucket", "vllm:inter_token_latency_seconds_bucket"),
        f"1 / clamp_min({QUANTILE}, 0.000001)",
    ),
    "inference.requests_finished": Expression(("vllm:request_success_total",), RATE),
    "inference.preemptions": Expression(("vllm:num_preemptions_total",), RATE),
    "inference.prompt_tokens_per_request": Expression(("vllm:request_prompt_tokens_bucket",), QUANTILE),
    "inference.generation_tokens_per_request": Expression(("vllm:request_generation_tokens_bucket",), QUANTILE),
}


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def matcher(label: str, values: tuple[str, ...]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return f'{label}="{escape(values[0])}"'
    return f'{label}=~"{"|".join(escape(v) for v in values)}"'


def device_class_values(name: str) -> tuple[str, ...]:
    """The `modelName` values a hangar device class is emitted under.

    DCGM has never heard of a hangar class, so one class expands into the vendor
    strings `libs/packages/contract/devices.json` maps onto it. A class the table does not
    carry falls back to matching its own name literally, which selects nothing
    until the table grows — an unscoped selector here would report the whole
    fleet as one card.
    """
    values = table().aliases(name, DIALECT_DCGM)
    if values:
        return values
    logger.warning("metrics.device_class_unmapped class=%s", name)
    return (name,)


def selector(scope: Scope) -> str:
    """The label matchers for one scope, cluster first and never absent.

    A scope whose source has no cluster label — a Prometheus that only holds one
    cluster's data — yields no cluster matcher, which is correct there and only
    there.
    """
    parts: list[str] = []
    if scope.cluster:
        parts.append(matcher(LABELS["cluster"], (scope.cluster,)))
    for dimension, values in scope.match.items():
        label = LABELS.get(dimension)
        if not label or not values:
            continue
        if dimension == "device_class":
            values = tuple(v for name in values for v in device_class_values(name))
        parts.append(matcher(label, values))
    return ",".join(p for p in parts if p)


def render(metric: Metric, series: str, where: str, window: str, by: str = "") -> str:
    """The PromQL that answers one metric out of one candidate series."""
    expression = DIALECT[metric.name]
    if expression.template:
        return expression.template.replace("$SERIES", series).replace("$SELECTOR", where).replace("$WINDOW", window)
    inner = f"{series}{{{where}}}" if where else series
    if by:
        return f"{metric.aggregate} by ({by}) ({inner})"
    return f"{metric.aggregate}({inner})"
