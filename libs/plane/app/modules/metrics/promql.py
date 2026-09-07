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
from .objectives import AVAILABILITY_2026_1
from .objectives import AVAILABILITY_OBJECTIVES
from .objectives import Objective


logger = logging.getLogger(__name__)

LABELS: dict[str, str] = {
    "cluster": "cluster",
    "node": "hostname",
    "device": "UUID",
    "device_class": "modelName",
    "deployment": "model_name",
    "worker": "dynamo_namespace",
    "endpoint": "hangar_endpoint",
}
"""The label each Hangar dimension is selected by. Verified against the fleet's store.

`dynamo_namespace` is not a namespace in the Kubernetes sense and is not the
`dynamo-namespace` pod label either. Dynamo composes it at runtime from
`DYN_NAMESPACE` and `DYN_NAMESPACE_WORKER_SUFFIX`, which reach the plane as the
`dynamo-namespace` and `dynamo-worker-hash` labels — see `worker_identity`. It is
the only label on a vLLM series that distinguishes one deployment of a model from
another.

`hangar_endpoint` is a plane uid rather than a name, and the gateway puts
`endpoint_name` beside it for panels. Joining on the name would break the moment
an endpoint is renamed, which is one of the things having an endpoint object is
for.
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

    `grouped` is the same expression with `$BY` where the label a breakdown groups
    by goes, and it has to be written out rather than derived: `sum by (x)` sits
    outside a rate and inside a `histogram_quantile`'s own `by (le)`, and a ratio
    needs it on both halves or the division matches nothing. Empty means this
    metric does not appear in a breakdown, which is the truthful outcome for a
    bare-series metric that never needed one.
    """

    candidates: tuple[str, ...]
    template: str = ""
    grouped: str = ""


QUANTILE = "histogram_quantile(0.95, sum by (le) (rate($SERIES{$SELECTOR}[$WINDOW])))"
P99 = "histogram_quantile(0.99, sum by (le) (rate($SERIES{$SELECTOR}[$WINDOW])))"
P90 = "histogram_quantile(0.90, sum by (le) (rate($SERIES{$SELECTOR}[$WINDOW])))"
P50 = "histogram_quantile(0.50, sum by (le) (rate($SERIES{$SELECTOR}[$WINDOW])))"
RATE = "sum(rate($SERIES{$SELECTOR}[$WINDOW]))"

QUANTILE_BY = "histogram_quantile(0.95, sum by (le, $BY) (rate($SERIES{$SELECTOR}[$WINDOW])))"
P50_BY = "histogram_quantile(0.50, sum by (le, $BY) (rate($SERIES{$SELECTOR}[$WINDOW])))"
RATE_BY = "sum by ($BY) (rate($SERIES{$SELECTOR}[$WINDOW]))"
GAUGE_BY = "sum by ($BY) ($SERIES{$SELECTOR})"
"""The grouped halves of the four shapes a fleet answer is built from.

A quantile keeps `le` alongside the grouping label because `histogram_quantile`
reads the buckets out of it; dropping it there returns nothing rather than a
wrong number, which is why this is written out per shape instead of derived.
"""

GATEWAY_REQUESTS = "hangar_gateway_requests_total"
GATEWAY_DURATION = "hangar_gateway_request_duration_seconds_bucket"
GATEWAY_TTFT = "hangar_gateway_ttft_seconds_bucket"
GATEWAY_TOKENS = "hangar_gateway_tokens_total"
GATEWAY_IN_FLIGHT = "hangar_gateway_requests_in_flight"

INPUT = 'direction="input"'
CACHED_INPUT = 'direction="cached_input"'
"""The two token directions a cache-hit ratio is built from. `output` is not one
of them, and `cached_input` is a subset of `input` rather than a sibling."""

LIVE = 'origin="live"'
"""Every gateway query carries it. The conformance suite sends deliberately
malformed requests at a live endpoint, so counting its probes would manufacture
the availability breach they exist to detect."""

FAILED = AVAILABILITY_2026_1.failed_matcher
"""The outcomes that are ours, matched by name and never by status class.

Rendered from the frozen objectives table (`objectives.py`) rather than written
here, so the number and the definition a quota names cannot drift apart. See
`libs/gateway/internal/metrics/outcome.go`, which is where the set is closed.
"""

COUNTED = AVAILABILITY_2026_1.counted_matcher
"""A caller that hung up took its own request away; charging it to the endpoint
would make an availability number a function of client behaviour."""


def _gateway_where(*extra: str) -> str:
    return ",".join(("$SELECTOR", LIVE) + extra)


def _gateway_rate(series: str, *extra: str) -> Expression:
    inner = f"rate($SERIES{{{_gateway_where(*extra)}}}[$WINDOW])"
    return Expression((series,), f"sum({inner})", f"sum by ($BY) ({inner})")


def _gateway_quantile(series: str, quantile: str) -> Expression:
    inner = f"rate($SERIES{{{_gateway_where()}}}[$WINDOW])"
    return Expression(
        (series,),
        f"histogram_quantile({quantile}, sum by (le) ({inner}))",
        f"histogram_quantile({quantile}, sum by (le, $BY) ({inner}))",
    )


def _gateway_gauge(series: str) -> Expression:
    inner = f"$SERIES{{{_gateway_where()}}}"
    return Expression((series,), f"sum({inner})", f"sum by ($BY) ({inner})")


def _failure_ratio(group: str, objective: Objective = AVAILABILITY_2026_1) -> str:
    """Failed requests over counted requests, as a percentage, per group or in total.

    Two different shapes of nothing meet in this one expression and a plain
    division confuses both of them.

    A healthy endpoint emits no failed-outcome series at all — the gateway only
    creates a child series once that outcome happens — and `sum` over an empty
    vector is empty, not zero, so the division would vanish exactly when
    everything works and would only produce a number once something broke. The
    numerator is therefore unioned with a zeroed copy of the denominator, which
    exists wherever the denominator does; a served window with no failures reads
    0% failed and 100% available, which is measured rather than assumed.

    A window nothing called, by contrast, has both rates at zero over series that
    are still being scraped from earlier traffic. Dividing that — clamped
    denominator or not — answers 0% failed and 100% available for a period nothing
    measured, which is the invented SLA this module exists to refuse. So the
    denominator is filtered to `> 0` rather than clamped: with no counted requests
    in the window there is no series, the service reports the sample as absent, and
    the console says it was not measured. An untouched endpoint is not 100%
    available and it is not 0% either.
    """
    aggregate = f"sum by ({group})" if group else "sum"
    failed = f"{aggregate} (rate($SERIES{{{_gateway_where(objective.failed_matcher)}}}[$WINDOW]))"
    counted = f"{aggregate} (rate($SERIES{{{_gateway_where(objective.counted_matcher)}}}[$WINDOW]))"
    return f"({failed} or ({counted} * 0)) / ({counted} > 0) * 100"


def _cache_hit_ratio(group: str) -> str:
    """Cached input tokens over input tokens, as a percentage, per group or in total.

    Sound as a ratio because cached input is a *subset* of input rather than a
    fourth kind of token — the gateway reads all three off the upstream's own
    usage object, so the denominator already contains the numerator and the
    result cannot exceed 100%.

    The numerator needs none of the union `_failure_ratio` uses: the gateway
    records `cached_input` on every request that carried a usage object, zero
    included, so the series exists wherever `input` does. The denominator is
    still filtered to `> 0` for the same reason as there — a window nothing
    called has both rates at zero over series still being scraped from earlier
    traffic, and answering 0% cached for a period nothing measured is the
    invented number this module refuses. No series, no sample, and the console
    says it was not measured.
    """
    aggregate = f"sum by ({group})" if group else "sum"
    cached = f"{aggregate} (rate($SERIES{{{_gateway_where(CACHED_INPUT)}}}[$WINDOW]))"
    served = f"{aggregate} (rate($SERIES{{{_gateway_where(INPUT)}}}[$WINDOW]))"
    return f"{cached} / ({served} > 0) * 100"


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
    "inference.ttft": Expression(("vllm:time_to_first_token_seconds_bucket",), QUANTILE, QUANTILE_BY),
    "inference.tpot": Expression(
        ("vllm:time_per_output_token_seconds_bucket", "vllm:inter_token_latency_seconds_bucket"),
        QUANTILE,
    ),
    "inference.latency": Expression(("vllm:e2e_request_latency_seconds_bucket",), QUANTILE),
    "inference.output_tokens_per_second": Expression(
        ("vllm:generation_tokens_total",), "sum(rate($SERIES{$SELECTOR}[$WINDOW]))", RATE_BY
    ),
    "inference.input_tokens_per_second": Expression(
        ("vllm:prompt_tokens_total",), "sum(rate($SERIES{$SELECTOR}[$WINDOW]))"
    ),
    "inference.requests_running": Expression(("vllm:num_requests_running",), "sum($SERIES{$SELECTOR})", GAUGE_BY),
    "inference.requests_waiting": Expression(("vllm:num_requests_waiting",), "sum($SERIES{$SELECTOR})"),
    "inference.kv_cache_utilization": Expression(
        ("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc"), "avg($SERIES{$SELECTOR}) * 100"
    ),
    "inference.prefix_cache_hit_rate": Expression(
        ("vllm:prefix_cache_hits_total",),
        "sum(rate($SERIES{$SELECTOR}[$WINDOW])) / clamp_min(sum(rate(vllm:prefix_cache_queries_total{$SELECTOR}[$WINDOW])), 1) * 100",
    ),
    "inference.ttft_p50": Expression(("vllm:time_to_first_token_seconds_bucket",), P50, P50_BY),
    "inference.ttft_p90": Expression(("vllm:time_to_first_token_seconds_bucket",), P90),
    "inference.ttft_p99": Expression(("vllm:time_to_first_token_seconds_bucket",), P99),
    "inference.tpot_p50": Expression(
        ("vllm:time_per_output_token_seconds_bucket", "vllm:inter_token_latency_seconds_bucket"),
        P50,
    ),
    "inference.tpot_p99": Expression(
        ("vllm:time_per_output_token_seconds_bucket", "vllm:inter_token_latency_seconds_bucket"),
        P99,
    ),
    "inference.latency_p99": Expression(("vllm:e2e_request_latency_seconds_bucket",), P99),
    "inference.queue_time": Expression(("vllm:request_queue_time_seconds_bucket",), QUANTILE),
    "inference.prefill_time": Expression(("vllm:request_prefill_time_seconds_bucket",), QUANTILE),
    "inference.decode_time": Expression(("vllm:request_decode_time_seconds_bucket",), QUANTILE),
    "inference.output_tokens_per_stream": Expression(
        ("vllm:time_per_output_token_seconds_bucket", "vllm:inter_token_latency_seconds_bucket"),
        f"1 / clamp_min({QUANTILE}, 0.000001)",
    ),
    "inference.requests_finished": Expression(("vllm:request_success_total",), RATE, RATE_BY),
    "inference.preemptions": Expression(("vllm:num_preemptions_total",), RATE),
    "inference.prompt_tokens_per_request": Expression(("vllm:request_prompt_tokens_bucket",), QUANTILE),
    "inference.generation_tokens_per_request": Expression(("vllm:request_generation_tokens_bucket",), QUANTILE),
    "serving.requests_per_second": _gateway_rate(GATEWAY_REQUESTS),
    "serving.latency_p50": _gateway_quantile(GATEWAY_DURATION, "0.50"),
    "serving.latency_p95": _gateway_quantile(GATEWAY_DURATION, "0.95"),
    "serving.latency_p99": _gateway_quantile(GATEWAY_DURATION, "0.99"),
    "serving.ttft_p50": _gateway_quantile(GATEWAY_TTFT, "0.50"),
    "serving.ttft_p95": _gateway_quantile(GATEWAY_TTFT, "0.95"),
    "serving.requests_in_flight": _gateway_gauge(GATEWAY_IN_FLIGHT),
    "serving.input_tokens_per_second": _gateway_rate(GATEWAY_TOKENS, INPUT),
    "serving.output_tokens_per_second": _gateway_rate(GATEWAY_TOKENS, 'direction="output"'),
    "serving.cache_hit_ratio": Expression((GATEWAY_TOKENS,), _cache_hit_ratio(""), _cache_hit_ratio("$BY")),
}
"""`direction="cached_input"` is never summed into either token rate. The gateway
records all three from the upstream's own usage object, and cached input is a
subset of input rather than a fourth kind of token — adding it double-counts the
prompt."""

for _objective in AVAILABILITY_OBJECTIVES.values():
    DIALECT[_objective.failure_metric] = Expression(
        (GATEWAY_REQUESTS,), _failure_ratio("", _objective), _failure_ratio("$BY", _objective)
    )
    DIALECT[_objective.availability_metric] = Expression(
        (GATEWAY_REQUESTS,),
        f"100 - ({_failure_ratio('', _objective)})",
        f"100 - ({_failure_ratio('$BY', _objective)})",
    )
"""Each objective's failure and availability metrics, rendered from its own entry.

Generated rather than written out so the outcome sets exist once: a hand-copied
regex here is how the denominator drifts away from the definition a quota names,
and a drifted denominator is a wrong availability number that renders perfectly.
"""


def is_quantile(name: str) -> bool:
    """Whether this metric is read out of a histogram's buckets rather than added up.

    A quantile cannot be combined with another quantile after the fact: two p95s
    measured in two stores have no mean, and the store that would know is the one
    holding both sets of buckets. A caller merging numbers across clusters asks
    this so it can leave the percentile out rather than average one into
    existence.
    """
    return "histogram_quantile" in DIALECT[name].template


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
    """The PromQL that answers one metric out of one candidate series.

    A scope that narrows nothing — the whole served surface, which is a real
    question — renders an empty `$SELECTOR`, and a matcher list may not begin with
    the comma that leaves behind. It is dropped here rather than by making every
    template carry a conditional.
    """
    expression = DIALECT[metric.name]
    template = expression.grouped if by and expression.grouped else expression.template
    if template:
        rendered = (
            template.replace("$SERIES", series)
            .replace("$SELECTOR", where)
            .replace("$WINDOW", window)
            .replace("$BY", by)
        )
        return rendered if where else rendered.replace("{,", "{")
    inner = f"{series}{{{where}}}" if where else series
    if by:
        return f"{metric.aggregate} by ({by}) ({inner})"
    return f"{metric.aggregate}({inner})"
