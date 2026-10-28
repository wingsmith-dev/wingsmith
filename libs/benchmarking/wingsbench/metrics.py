"""What each harness's own export calls the numbers we compare across harnesses.

A harness's result is stored exactly as the tool emitted it, and normalisation
happens only through an explicit map that says *for this harness, between these
versions, the P90 time-to-first-token is at this path, in these units*. No fuzzy
search, no shared output schema, and a path that does not resolve is a named miss
instead of a silent zero.

The plane's benchmarks module reads AIPerf with the same idea, in
`libs/plane/app/modules/benchmarks/summaries.py`. The two are separate copies by
design: this package has no dependencies and runs from a checkout without the
plane installed, so it cannot import the plane's module. What they share is a
contract, and nothing but review keeps the two copies to it:

- the field ids (`ttft.p90`, `request.errors`, ...) mean the same number in both
  places, so a local finding and a cluster benchmark can be put in one sentence;
- the AIPerf paths are the same paths, and a path that a harness leaves out
  intentionally means the same thing in both. AIPerf writes `error_request_count` only
  when a request failed, so its absence from an otherwise complete export is zero
  failures, not a missing reading. A copy that reads it as missing reports a gap
  on every clean run, and the two disagree on the same file.

`tests/test_metrics.py` pins the paths read here against a fixture of each
harness's export, two of them captured from AIPerf itself, so a drift on this side
fails a test instead of a chart. Nothing pins the plane's copy from here.

Three facts about these three harnesses make the map non-trivial, and each is why
`Field` carries what it carries:

- GuideLLM reports `request_latency` in **seconds** while every token timing in
  the same file is in milliseconds. `scale` converts, so a field id means one unit
  everywhere.
- AIPerf reports failed requests as `error_request_count`, and only when one
  failed. Its `error_summary` beside it is a list of error *types*, so its length
  is not a request count. `absent` says what a missing path means for a field that
  a harness leaves out when it is zero.
- Each harness splits its metrics differently: GuideLLM by outcome
  (successful/errored/incomplete/total), vLLM by percentile in a flat key. Reading
  GuideLLM's `.total` where we meant `.successful` would quietly mix failed
  requests into a latency figure, so every path here names `.successful`.

`DERIVED` is the deliberate exception to path-reading. Two numbers that matter for
a load curve are not in every export (per-stream output speed, and the input
length actually achieved), and computing them from two fields that *are* there
beats leaving a hole in the comparison. A derived value is recorded as derived and
the report labels it, because a computed number and a measured one are not the
same claim.
"""

from __future__ import annotations

from typing import Any
from typing import NamedTuple
from collections.abc import Callable


MILLISECONDS = "ms"
TOKENS_PER_SECOND = "tokens/sec"
REQUESTS_PER_SECOND = "requests/sec"
COUNT = "count"
TOKENS = "tokens"

SECONDS_TO_MS = 1000.0


class Field(NamedTuple):
    """One number an adapter knows how to find, and what it is measured in.

    `absent` is the value of a field the harness omits instead of reporting as
    zero. It applies only when the export itself is an object, so an unreadable
    export still reads as a miss, not as zero failures.
    """

    id: str
    path: tuple[str | int, ...]
    unit: str
    scale: float = 1.0
    absent: float | None = None


class Adapter(NamedTuple):
    """One harness's field map, valid over a half-open range of its versions.

    `max_version` is exclusive and `None` means open-ended. A run whose version
    falls outside every range for its harness is not read with a map written for a
    different one: the readings are missing, which is the whole reason the range
    is recorded.
    """

    harness: str
    min_version: str
    max_version: str | None
    fields: tuple[Field, ...]


class Reading(NamedTuple):
    """A resolved number, or the path that was not there."""

    value: float | None
    unit: str
    missing: str | None


TTFT_AVG = "ttft.avg"
TTFT_P50 = "ttft.p50"
TTFT_P90 = "ttft.p90"
TTFT_P99 = "ttft.p99"
ITL_AVG = "itl.avg"
ITL_P90 = "itl.p90"
E2E_AVG = "e2e.avg"
E2E_P50 = "e2e.p50"
E2E_P90 = "e2e.p90"
E2E_P99 = "e2e.p99"
OUTPUT_SPEED_PER_STREAM = "output_speed.per_stream"
OUTPUT_SPEED_TOTAL = "output_speed.total"
REQUEST_THROUGHPUT = "request.throughput"
REQUEST_COUNT = "request.count"
ERROR_COUNT = "request.errors"
INPUT_TOKENS_MEAN = "tokens.input_mean"
OUTPUT_TOKENS_MEAN = "tokens.output_mean"


UNITS: dict[str, str] = {
    TTFT_AVG: MILLISECONDS,
    TTFT_P50: MILLISECONDS,
    TTFT_P90: MILLISECONDS,
    TTFT_P99: MILLISECONDS,
    ITL_AVG: MILLISECONDS,
    ITL_P90: MILLISECONDS,
    E2E_AVG: MILLISECONDS,
    E2E_P50: MILLISECONDS,
    E2E_P90: MILLISECONDS,
    E2E_P99: MILLISECONDS,
    OUTPUT_SPEED_PER_STREAM: TOKENS_PER_SECOND,
    OUTPUT_SPEED_TOTAL: TOKENS_PER_SECOND,
    REQUEST_THROUGHPUT: REQUESTS_PER_SECOND,
    REQUEST_COUNT: COUNT,
    ERROR_COUNT: COUNT,
    INPUT_TOKENS_MEAN: TOKENS,
    OUTPUT_TOKENS_MEAN: TOKENS,
}

LABELS: dict[str, str] = {
    TTFT_AVG: "TTFT mean",
    TTFT_P50: "TTFT p50",
    TTFT_P90: "TTFT p90",
    TTFT_P99: "TTFT p99",
    ITL_AVG: "Inter-token mean",
    ITL_P90: "Inter-token p90",
    E2E_AVG: "Latency mean",
    E2E_P50: "Latency p50",
    E2E_P90: "Latency p90",
    E2E_P99: "Latency p99",
    OUTPUT_SPEED_PER_STREAM: "Output tok/s per stream",
    OUTPUT_SPEED_TOTAL: "Output tok/s total",
    REQUEST_THROUGHPUT: "Requests/s",
    REQUEST_COUNT: "Requests",
    ERROR_COUNT: "Errors",
    INPUT_TOKENS_MEAN: "Input tokens",
    OUTPUT_TOKENS_MEAN: "Output tokens",
}

COMPARABLE: tuple[str, ...] = (
    TTFT_P50,
    TTFT_P90,
    TTFT_P99,
    ITL_AVG,
    E2E_P50,
    E2E_P90,
    E2E_P99,
    OUTPUT_SPEED_PER_STREAM,
    OUTPUT_SPEED_TOTAL,
    REQUEST_THROUGHPUT,
    ERROR_COUNT,
)
"""The ids the report compares across harnesses.

Not every id: a harness's own request count agrees with the others by
construction, and putting that in a cross-harness table implies an agreement that
was never in question.
"""

LOWER_IS_BETTER: frozenset[str] = frozenset(
    {TTFT_AVG, TTFT_P50, TTFT_P90, TTFT_P99, ITL_AVG, ITL_P90, E2E_AVG, E2E_P50, E2E_P90, E2E_P99, ERROR_COUNT}
)


AIPERF_0_4 = Adapter(
    harness="aiperf",
    min_version="0.4.0",
    max_version=None,
    fields=(
        Field(TTFT_AVG, ("time_to_first_token", "avg"), MILLISECONDS),
        Field(TTFT_P50, ("time_to_first_token", "p50"), MILLISECONDS),
        Field(TTFT_P90, ("time_to_first_token", "p90"), MILLISECONDS),
        Field(TTFT_P99, ("time_to_first_token", "p99"), MILLISECONDS),
        Field(ITL_AVG, ("inter_token_latency", "avg"), MILLISECONDS),
        Field(ITL_P90, ("inter_token_latency", "p90"), MILLISECONDS),
        Field(E2E_AVG, ("request_latency", "avg"), MILLISECONDS),
        Field(E2E_P50, ("request_latency", "p50"), MILLISECONDS),
        Field(E2E_P90, ("request_latency", "p90"), MILLISECONDS),
        Field(E2E_P99, ("request_latency", "p99"), MILLISECONDS),
        Field(OUTPUT_SPEED_PER_STREAM, ("output_token_throughput_per_user", "avg"), TOKENS_PER_SECOND),
        Field(OUTPUT_SPEED_TOTAL, ("output_token_throughput", "avg"), TOKENS_PER_SECOND),
        Field(REQUEST_THROUGHPUT, ("request_throughput", "avg"), REQUESTS_PER_SECOND),
        Field(REQUEST_COUNT, ("request_count", "avg"), COUNT),
        Field(ERROR_COUNT, ("error_request_count", "avg"), COUNT, absent=0.0),
        Field(INPUT_TOKENS_MEAN, ("input_sequence_length", "avg"), TOKENS),
        Field(OUTPUT_TOKENS_MEAN, ("output_sequence_length", "avg"), TOKENS),
    ),
)
"""AIPerf's export. Every statistic hangs off the metric name as a sibling of `avg`,
including the ones that only have an `avg`: `request_throughput` is a rate over the
whole run and has no percentiles, so reading `avg` there reads the only number there
is. Failures are `error_summary`, a list, hence the one `length` field in the table.
"""


VLLM_BENCH_0_28 = Adapter(
    harness="vllm-bench",
    min_version="0.28.0",
    max_version=None,
    fields=(
        Field(TTFT_AVG, ("mean_ttft_ms",), MILLISECONDS),
        Field(TTFT_P50, ("p50_ttft_ms",), MILLISECONDS),
        Field(TTFT_P90, ("p90_ttft_ms",), MILLISECONDS),
        Field(TTFT_P99, ("p99_ttft_ms",), MILLISECONDS),
        Field(ITL_AVG, ("mean_itl_ms",), MILLISECONDS),
        Field(ITL_P90, ("p90_itl_ms",), MILLISECONDS),
        Field(E2E_AVG, ("mean_e2el_ms",), MILLISECONDS),
        Field(E2E_P50, ("p50_e2el_ms",), MILLISECONDS),
        Field(E2E_P90, ("p90_e2el_ms",), MILLISECONDS),
        Field(E2E_P99, ("p99_e2el_ms",), MILLISECONDS),
        Field(OUTPUT_SPEED_TOTAL, ("output_throughput",), TOKENS_PER_SECOND),
        Field(REQUEST_THROUGHPUT, ("request_throughput",), REQUESTS_PER_SECOND),
        Field(REQUEST_COUNT, ("completed",), COUNT),
        Field(ERROR_COUNT, ("failed",), COUNT),
    ),
)
"""`vllm bench serve`'s flat JSON: one level, keys built as `{stat}_{metric}_ms`.

Which of them exist is a function of the flags: `--percentile-metrics` decides
whether `e2el` appears at all and `--metric-percentiles` decides which percentiles
do. The runner always passes both explicitly, so this map is not aspirational; a
hand-run export made without them will show these ids as missing, which is the
correct answer for a file that does not contain them.

Per-stream output speed and the mean sequence lengths are not in the file. They are
derived below from `itl.avg` and from the totals it does report.
"""


def _guidellm(field_id: str, metric: str, stat: tuple[str, ...], unit: str, scale: float = 1.0) -> Field:
    """One GuideLLM metric, always from the `successful` split."""
    return Field(field_id, ("metrics", metric, "successful", *stat), unit, scale)


GUIDELLM_0_7 = Adapter(
    harness="guidellm",
    min_version="0.7.0",
    max_version="0.8.0",
    fields=(
        _guidellm(TTFT_AVG, "time_to_first_token_ms", ("mean",), MILLISECONDS),
        _guidellm(TTFT_P50, "time_to_first_token_ms", ("percentiles", "p50"), MILLISECONDS),
        _guidellm(TTFT_P90, "time_to_first_token_ms", ("percentiles", "p90"), MILLISECONDS),
        _guidellm(TTFT_P99, "time_to_first_token_ms", ("percentiles", "p99"), MILLISECONDS),
        _guidellm(ITL_AVG, "inter_token_latency_ms", ("mean",), MILLISECONDS),
        _guidellm(ITL_P90, "inter_token_latency_ms", ("percentiles", "p90"), MILLISECONDS),
        _guidellm(E2E_AVG, "request_latency", ("mean",), MILLISECONDS, SECONDS_TO_MS),
        _guidellm(E2E_P50, "request_latency", ("percentiles", "p50"), MILLISECONDS, SECONDS_TO_MS),
        _guidellm(E2E_P90, "request_latency", ("percentiles", "p90"), MILLISECONDS, SECONDS_TO_MS),
        _guidellm(E2E_P99, "request_latency", ("percentiles", "p99"), MILLISECONDS, SECONDS_TO_MS),
        _guidellm(OUTPUT_SPEED_TOTAL, "output_tokens_per_second", ("mean",), TOKENS_PER_SECOND),
        _guidellm(REQUEST_THROUGHPUT, "requests_per_second", ("mean",), REQUESTS_PER_SECOND),
        _guidellm(INPUT_TOKENS_MEAN, "prompt_token_count", ("mean",), TOKENS),
        _guidellm(OUTPUT_TOKENS_MEAN, "output_token_count", ("mean",), TOKENS),
        Field(REQUEST_COUNT, ("metrics", "request_totals", "successful"), COUNT),
        Field(ERROR_COUNT, ("metrics", "request_totals", "errored"), COUNT),
    ),
)
"""GuideLLM 0.7's export, one element of its `benchmarks` list at a time.

Every timing is split four ways by outcome and this map always takes `successful`,
so a run with failures reports a latency for the requests that worked and a
non-zero `request.errors` beside it, instead of one number that silently averages
the two. `request_latency` is the one field in seconds; `scale` is why the id still
means milliseconds. The version range closes at 0.8.0 because 0.7 was itself a
rewrite of the CLI and the result schema carries a `metadata.version` of its own:
the next such rewrite should be a new entry here, not a surprise in a chart.
"""


VLLM_SWEEP_0_28 = VLLM_BENCH_0_28._replace(harness="vllm-sweep")
"""The sweep writes `vllm bench serve`'s own result files, one per combination.

A separate entry instead of an alias because `harness` is half of what makes two
runs comparable: the sweep injects its own flags into the bench command, so a
number from it and a number from a hand-driven `vllm-bench` cell were produced
under different conditions even though the file format is identical.
"""


ADAPTERS: dict[str, tuple[Adapter, ...]] = {
    AIPERF_0_4.harness: (AIPERF_0_4,),
    VLLM_BENCH_0_28.harness: (VLLM_BENCH_0_28,),
    VLLM_SWEEP_0_28.harness: (VLLM_SWEEP_0_28,),
    GUIDELLM_0_7.harness: (GUIDELLM_0_7,),
}
"""Every harness the local runner can read.

A harness with no entry still runs and its raw export is still stored: it just
contributes nothing to the cross-harness comparison, and the report says so by
name, not by omission.
"""


Formula = tuple[str, Callable[[dict[str, float]], float | None]]


def _per_stream_from_itl(readings: dict[str, float]) -> float | None:
    itl = readings.get(ITL_AVG)
    if not itl or itl <= 0:
        return None
    return 1000.0 / itl


DERIVED: dict[str, Formula] = {
    OUTPUT_SPEED_PER_STREAM: ("1000 / itl.avg", _per_stream_from_itl),
}
"""Fields worth having that not every harness reports, and how they are computed.

Applied only when the direct path is missing, and recorded as derived so the report
can mark the number. Per-stream output speed is the one that matters most for a
load curve: it is what a single caller feels as the deployment fills up, and the
aggregate throughput it is so easily confused with rises while it falls.

`1000 / itl.avg` is the decode rate one stream sustains between tokens. It is not
identical to AIPerf's measured `output_token_throughput_per_user`, which divides a
request's output tokens by its whole duration and so carries the prefill wait. The
two agree to a few percent at long output lengths and diverge at short ones; the
report labels the derived one instead of pretending they are the same measurement.
"""

_TOTALS: dict[str, str] = {
    INPUT_TOKENS_MEAN: "total_input_tokens",
    OUTPUT_TOKENS_MEAN: "total_output_tokens",
}
"""Harness keys holding a run total where we want a per-request mean.

Only `vllm-bench` needs this, and its own gotcha is the reason to bother: the
random dataset's realised input length exceeds what was asked for, so the right
input length for a cross-harness comparison is the one it reports having sent,
divided by the requests that completed, never the flag value.
"""


def _version_key(version: str) -> tuple[int, ...]:
    """A dotted version as a comparable tuple, tolerant of suffixes like `1.2.0rc1`."""
    parts: list[int] = []
    for chunk in version.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def adapter_for(harness: str, version: str) -> Adapter | None:
    """The field map for this harness at this version, or None when we have none."""
    target = _version_key(version)
    for adapter in ADAPTERS.get(harness, ()):
        if target < _version_key(adapter.min_version):
            continue
        if adapter.max_version is not None and target >= _version_key(adapter.max_version):
            continue
        return adapter
    return None


def _resolve(summary: Any, path: tuple[str | int, ...]) -> Any:
    """Walk a path through the harness's own export, or give up."""
    node: Any = summary
    for step in path:
        if isinstance(step, int):
            if not isinstance(node, list) or len(node) <= step:
                return None
            node = node[step]
            continue
        if not isinstance(node, dict) or step not in node:
            return None
        node = node[step]
    return node


def _number(node: Any, field: Field) -> float | None:
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        return None
    return float(node) * field.scale


def read(harness: str, version: str, summary: Any) -> dict[str, Reading]:
    """Every canonical field this harness's export can answer, resolved.

    A field the map declares but the export does not hold is present in the result
    with a `missing` path, not absent, so a report can say which harness
    could not answer which question instead of quietly comparing a subset.
    """
    adapter = adapter_for(harness, version)
    if adapter is None:
        return {}
    readings: dict[str, Reading] = {}
    for field in adapter.fields:
        node = _resolve(summary, field.path)
        if node is None and field.absent is not None and isinstance(summary, dict) and summary:
            value: float | None = field.absent
        else:
            value = _number(node, field)
        dotted = ".".join(str(step) for step in field.path)
        readings[field.id] = Reading(value, field.unit, None if value is not None else dotted)
    return readings


def derive(summary: Any, readings: dict[str, float]) -> dict[str, tuple[float, str]]:
    """Fields computed from other fields, for the harnesses that do not report them.

    Returns `{field id: (value, how)}`, where `how` is the formula, so the caller
    records what it did instead of presenting a computed number as a measured one.
    Never overwrites a direct reading.
    """
    out: dict[str, tuple[float, str]] = {}
    for field_id, (label, formula) in DERIVED.items():
        if field_id in readings:
            continue
        value = formula(readings)
        if value is not None:
            out[field_id] = (value, label)
    for field_id, total_key in _TOTALS.items():
        if field_id in readings:
            continue
        total = _resolve(summary, (total_key,))
        count = readings.get(REQUEST_COUNT)
        if isinstance(total, (int, float)) and not isinstance(total, bool) and count:
            out[field_id] = (float(total) / count, f"{total_key} / request.count")
    return out
