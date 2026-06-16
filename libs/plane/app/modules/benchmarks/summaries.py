"""What one harness's verbatim `summary` calls the numbers we grade against.

`BenchmarkRun.summary` is stored exactly as the harness emitted it and nothing
normalises it, which is the rule and stays the rule. This module is the other
half of that rule: refusing to assert a union across formats does not mean
refusing to be able to read one. An entry here says *for this harness, between
these versions, the P90 time-to-first-token is at this path, in these units* —
a reader's knowledge of one producer, per version, and never a shared schema.

Per-version and explicit rather than a fuzzy search, because a harness renaming
a percentile between releases is precisely the failure the `harness_version`
comparability rule exists to catch, and a lookup that guessed would paper over
it. A path that does not resolve is a named miss, never a silent zero.
"""

from typing import Any
from typing import NamedTuple


MILLISECONDS = "ms"
TOKENS_PER_SECOND = "tokens/sec"
REQUESTS_PER_SECOND = "requests/sec"
COUNT = "count"
RATIO = "ratio"


class Field(NamedTuple):
    """One number an adapter knows how to find, and what it is measured in."""

    id: str
    path: tuple[str, ...]
    unit: str


class Adapter(NamedTuple):
    """One harness's field map, valid over a half-open range of its versions.

    `max_version` is exclusive and `None` means open-ended. A run whose version
    falls outside every range for its harness is not graded against a map
    written for a different one — it is `inconclusive`, which is the whole
    reason the range is recorded.

    `display` is the half of the map a verdict cannot see. `read` searches
    `fields` and nothing else, so a number named here is readable for a chart and
    is a named miss to a check — which is the point. The aggregate output rate is
    the reason the split exists: it is the y-axis of every load curve, and it
    clears a per-stream floor under every possible condition, so a check that
    could reach it would be a check that can never fail.
    """

    harness: str
    min_version: str
    max_version: str | None
    fields: tuple[Field, ...]
    display: tuple[Field, ...] = ()


class Reading(NamedTuple):
    """A resolved number, or the dotted path that was not there."""

    value: float | None
    unit: str
    missing: str | None


TTFT_P90 = "ttft.p90"
TTFT_P50 = "ttft.p50"
TTFT_AVG = "ttft.avg"
OUTPUT_SPEED_PER_STREAM = "output_speed.per_stream"
REQUEST_COUNT = "request.count"
ERROR_COUNT = "request.errors"

TTFT_P95 = "ttft.p95"
TTFT_P99 = "ttft.p99"
ITL_P50 = "itl.p50"
E2E_P95 = "e2e.p95"
OUTPUT_SPEED_TOTAL = "output_speed.total"
REQUEST_THROUGHPUT = "request.throughput"

DISPLAY_ONLY: frozenset[str] = frozenset({TTFT_P95, TTFT_P99, ITL_P50, E2E_P95, OUTPUT_SPEED_TOTAL, REQUEST_THROUGHPUT})
"""Every id a chart may read and no verdict may.

Not the mechanism — `read` searching only `fields` is the mechanism — but the
assertion a test holds that mechanism to.
"""

PROBE_TOTAL = "probe.total"
PROBE_PASSED = "probe.passed"
PROBE_FAILED = "probe.failed"
PROBE_SKIPPED = "probe.skipped"
PROBE_ERRORED = "probe.errored"


AIPERF_0_4 = Adapter(
    harness="aiperf",
    min_version="0.4.0",
    max_version=None,
    fields=(
        Field(TTFT_P90, ("time_to_first_token", "p90"), MILLISECONDS),
        Field(TTFT_P50, ("time_to_first_token", "p50"), MILLISECONDS),
        Field(TTFT_AVG, ("time_to_first_token", "avg"), MILLISECONDS),
        # Confirmed against a real export of aiperf 0.12.0. The aggregate
        # `output_token_throughput` is not a fallback here and never becomes one: a serving
        # GPU's aggregate rate clears a per-stream floor under every possible condition, so a
        # check that could reach it would be a check that can never fail. It is a number worth
        # drawing all the same — flattening aggregate throughput is what a knee looks like —
        # so it sits in `display` below, which `read` cannot see.
        Field(OUTPUT_SPEED_PER_STREAM, ("output_token_throughput_per_user", "avg"), TOKENS_PER_SECOND),
        Field(REQUEST_COUNT, ("request_count", "avg"), COUNT),
        Field(ERROR_COUNT, ("error_count",), COUNT),
    ),
    display=(
        Field(TTFT_P95, ("time_to_first_token", "p95"), MILLISECONDS),
        Field(TTFT_P99, ("time_to_first_token", "p99"), MILLISECONDS),
        Field(ITL_P50, ("inter_token_latency", "p50"), MILLISECONDS),
        Field(E2E_P95, ("request_latency", "p95"), MILLISECONDS),
        Field(OUTPUT_SPEED_TOTAL, ("output_token_throughput", "avg"), TOKENS_PER_SECOND),
        Field(REQUEST_THROUGHPUT, ("request_throughput", "avg"), REQUESTS_PER_SECOND),
    ),
)

CONFORMANCE_0_1 = Adapter(
    harness="conformance",
    min_version="0.1.0",
    max_version=None,
    fields=(
        Field(PROBE_TOTAL, ("counts", "total"), COUNT),
        Field(PROBE_PASSED, ("counts", "pass"), COUNT),
        Field(PROBE_FAILED, ("counts", "fail"), COUNT),
        Field(PROBE_SKIPPED, ("counts", "skip"), COUNT),
        Field(PROBE_ERRORED, ("counts", "error"), COUNT),
    ),
)

EVALSCOPE_1_11 = Adapter(
    harness="evalscope",
    min_version="1.11.0",
    max_version=None,
    fields=(),
)
"""No flat fields: an accuracy summary is keyed by dataset and the dataset set is the
request's, not the adapter's. `dataset_scores` reads it, and the version range is still
what says whether this reader understands the export at all.
"""


ADAPTERS: dict[str, tuple[Adapter, ...]] = {
    AIPERF_0_4.harness: (AIPERF_0_4,),
    CONFORMANCE_0_1.harness: (CONFORMANCE_0_1,),
    EVALSCOPE_1_11.harness: (EVALSCOPE_1_11,),
}


def adapter_for(harness: str, harness_version: str | None) -> Adapter | None:
    """The map written for this harness at this version, or nothing.

    Nothing when the harness has no map, when the run never reported a version,
    or when the version it reported falls outside every declared range. All
    three end at the same place — a verdict of `inconclusive` — because all
    three mean the same thing: we cannot say we know what these numbers are.
    """
    if harness_version is None:
        return None
    version = _parse(harness_version)
    if version is None:
        return None
    for adapter in ADAPTERS.get(harness, ()):
        low = _parse(adapter.min_version) or (0, 0, 0)
        if version < low:
            continue
        if adapter.max_version is not None:
            high = _parse(adapter.max_version)
            if high is not None and version >= high:
                continue
        return adapter
    return None


def read(adapter: Adapter, summary: dict | None, field_id: str) -> Reading:
    """One gradeable field of one summary, or the dotted path that was not in it.

    Searches `adapter.fields` and nothing else. A display id asked for here is
    the same named miss an unknown id is, which is what keeps a display-only
    number structurally out of every verdict rather than out of it by memory.
    """
    field = next((candidate for candidate in adapter.fields if candidate.id == field_id), None)
    if field is None:
        return Reading(None, "", field_id)
    return _resolve(field, summary)


def show(adapter: Adapter, summary: dict | None, field_id: str) -> Reading:
    """One field a chart may read, from either half of the map.

    The inverse of `read` and deliberately not its replacement: a curve plots a
    graded percentile and an ungraded aggregate side by side through one call,
    while grading stays one-way.
    """
    field = next(
        (candidate for candidate in adapter.display + adapter.fields if candidate.id == field_id),
        None,
    )
    if field is None:
        return Reading(None, "", field_id)
    return _resolve(field, summary)


def _resolve(field: Field, summary: dict | None) -> Reading:
    cursor: Any = summary or {}
    for step in field.path:
        if not isinstance(cursor, dict) or step not in cursor:
            return Reading(None, field.unit, ".".join(field.path))
        cursor = cursor[step]
    if isinstance(cursor, bool) or not isinstance(cursor, (int, float)):
        return Reading(None, field.unit, ".".join(field.path))
    return Reading(float(cursor), field.unit, None)


def failures(summary: dict | None) -> list[dict]:
    """The enumerated probe failures a conformance summary carries.

    A list rather than a count because the required/advisory split is per entry
    and the verdict grades only the required ones. The full per-probe record —
    every probe, passing and failing, with its latency — is telemetry and lives
    behind `artifacts_uri`, never here.
    """
    listed = (summary or {}).get("failures")
    if not isinstance(listed, list):
        return []
    return [entry for entry in listed if isinstance(entry, dict)]


def dataset_scores(summary: dict | None) -> dict[str, tuple[str, float]]:
    """Every `dataset -> (metric, score)` an accuracy summary reports.

    Entries that do not carry a numeric score are dropped rather than defaulted,
    which leaves the dataset looking absent — and an absent dataset is graded
    `not_applicable` rather than passed, which is the safe direction.
    """
    results = (summary or {}).get("results")
    if not isinstance(results, dict):
        return {}
    scores: dict[str, tuple[str, float]] = {}
    for dataset, entry in results.items():
        if not isinstance(entry, dict):
            continue
        score = entry.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        metric = entry.get("metric")
        scores[str(dataset)] = (str(metric) if metric else "accuracy", float(score))
    return scores


def _parse(version: str) -> tuple[int, int, int] | None:
    parts = version.strip().lstrip("vV").split(".")[:3]
    numbers: list[int] = []
    for part in parts:
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            return None
        numbers.append(int(digits))
    if not numbers:
        return None
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]
