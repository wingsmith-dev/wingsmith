"""A stepped request's runs read back as a load curve, and the knee of it.

A ramp already exists as N rows carrying a `step_index` and the load each step
applied, and nothing reads them as one thing. `buckets.py` groups a sweep by the
band each run landed in, because the standard sets a target per band; this
module orders a ramp by the load each run applied, because the question a ramp
is asked is where latency stops being bought by throughput.

Pure functions and nothing else: no session, no ORM, no I/O. A curve and its
knee are `f(runs, criteria)` by construction, which is what makes them
derivable at read against the pinned criteria version, not stored beside
the runs. Nothing here is written anywhere: there is no curve table, no knee
column and no migration, for `verdicts.py`'s reason. A knee is a judgement about
a set of runs, not a phase of one.

**The x-axis is the load, not the step index.** `services._steps` collapses
repeats and preserves the caller's order, so a request asking for `[16, 4, 64]`
has its lowest load at index 1. The response lists points by `step_index`
because that is the order the request declared them in and the console draws
what it was asked for; the knee is read along ascending load, because a curve
walked in creation order puts the elbow in the wrong place.

The chart's second axis is the aggregate output rate, which is a number no
verdict may read: it lives in `summaries.Adapter.display` and is reached
through `summaries.show`. A curve is a picture, not a claim about acceptance.
"""

from typing import Any
from typing import NamedTuple
from collections.abc import Sequence

from . import buckets
from . import verdicts
from . import summaries


CONCURRENCY = "concurrency"
INPUT_TOKENS_MEAN = "input_tokens_mean"

STEP_FIELDS: dict[str, str] = {"ramp": CONCURRENCY, "sweep": INPUT_TOKENS_MEAN}
"""Which declared parameter a stepped mode walks, duplicated from the expansion.

Two copies and for `buckets.py`'s reason: the expansion's table says
what a request was created against, and a read sharing the constant would follow
it silently if somebody added a mode. A test holds the two equal.
"""

SUCCEEDED = "succeeded"

FIRST_CROSSING = "first-crossing"
"""The only method implemented, and it is named on every answer.

A saturation method reading the aggregate throughput flattening is a different
question with a different answer, and blending the two into one number would
produce a knee nobody can attribute. If one is added it takes its own name here
and the answer says which it was.
"""

KNEE_METRIC = "ttft_p90_ms"
"""The reading the knee is read off, and it is the one the standard anchors.

P90 instead of the P95 a chart might prefer: the threshold has to come from the
pinned criteria version and `Criteria.ttft` sets a P90, a P50 and an average
ceiling per band. A knee read against a threshold this plane invented would be a
number with no standard behind it.
"""


class Series(NamedTuple):
    """One reading a curve carries, and the field map entry it is read through."""

    key: str
    field_id: str


SERIES: tuple[Series, ...] = (
    Series("ttft_p50_ms", summaries.TTFT_P50),
    Series("ttft_p90_ms", summaries.TTFT_P90),
    Series("ttft_p95_ms", summaries.TTFT_P95),
    Series("ttft_p99_ms", summaries.TTFT_P99),
    Series("itl_p50_ms", summaries.ITL_P50),
    Series("e2e_p95_ms", summaries.E2E_P95),
    Series("output_tps_per_stream", summaries.OUTPUT_SPEED_PER_STREAM),
    Series("output_tps_total", summaries.OUTPUT_SPEED_TOTAL),
    Series("request_throughput", summaries.REQUEST_THROUGHPUT),
)
"""Every reading a point carries, graded and display-only side by side.

`output_tps_total` and `request_throughput` are display-only and are reached
through `summaries.show`, which is the only reader that can see them. Reading
them through `read` would be a named miss, and no check names them at all.
"""


class Value(NamedTuple):
    """One reading of one run, or the dotted path that was not in its summary.

    A miss is `value=None` beside the path that was looked for, never a zero: a
    curve with a silent zero at one step draws a cliff the machine did not have.
    """

    value: float | None
    unit: str
    path: str


class Point(NamedTuple):
    """One run of a request, reduced to what a curve needs of it.

    A discarded run stays on the curve instead of being dropped, a warm-up is
    part of what was run and the console says so, and is excluded from the
    knee. An open run is here too, with every reading null, so a curve can be
    drawn while the ramp is still climbing.

    `serving_digest` is what the run measured, and it is on the point rather
    than only on the response because the knee needs it: a request whose steps
    landed on two configurations still has one graded run per step, and a
    crossing read across them would be attributed to a machine that never ran.
    """

    run_id: str
    step_index: int
    step_value: int | None
    input_tokens: int | None
    serving_digest: str | None
    phase: str
    discard: bool
    readings: dict[str, Value]


class Knee(NamedTuple):
    """Where the criteria-anchored metric first crossed its ceiling, or why it cannot be said.

    `found` is false far more often than it is true, and every false carries its
    own reason: a ramp that never crossed, a ramp that started above the
    ceiling, a ramp too short to have a crossing at all and a curve that crossed
    back under are four different answers, and collapsing them into one silence
    would leave an operator to guess which.
    """

    found: bool
    step_index: int | None
    step_value: int | None
    method: str
    metric: str
    threshold: float | None
    reason: str


def points(runs: Sequence[Any], step_field: str) -> list[Point]:
    """One request's runs as ordered curve points, by `step_index` ascending.

    The load comes from each run's own declared parameters and never from its
    summary, for `buckets.sample`'s reason: the summary is verbatim and is under
    no obligation to say what was asked of it.

    A run that did not succeed carries null readings whatever its summary holds.
    A failed harness reports the fraction of the load it got through, and a
    partial summary drawn as a point is a dip that never happened.
    """
    assembled = [_point(run, step_field) for run in runs]
    return sorted(assembled, key=lambda entry: entry.step_index)


def knee(curve: Sequence[Point], criteria: verdicts.Criteria, step_field: str = CONCURRENCY) -> Knee:
    """The first step at which the anchored metric crossed its band's ceiling.

    Read along ascending load instead of ascending step index, because a
    request may declare its steps in any order and the elbow is a property of
    the load. Discarded and unsucceeded runs are excluded outright: a knee is a
    statement about measurements, and neither of those is one.
    """
    graded = [entry for entry in curve if not entry.discard and entry.phase == SUCCEEDED]

    repeated = _repeated_steps(graded)
    if repeated is not None:
        index, count = repeated
        return _absent(
            None,
            f"step {index} has {count} runs, one per placed target, "
            "and a curve drawn through them describes no single machine",
        )

    if len(graded) < 2:
        return _absent(None, f"{len(graded)} of the request's runs succeeded; a crossing needs at least two")

    measured = {entry.serving_digest for entry in graded if entry.serving_digest}
    if len(measured) > 1:
        return _absent(
            None,
            f"the steps measured {len(measured)} serving configurations, "
            "and a curve drawn through them describes no single machine",
        )

    unloaded = next((entry for entry in graded if entry.step_value is None), None)
    if unloaded is not None:
        return _absent(None, f"run {unloaded.run_id} declared no {step_field}, so its point has no place on the curve")

    bands = {buckets.band_for(entry.input_tokens) for entry in graded}
    if len(bands) > 1:
        return _absent(
            None,
            "the steps landed in the input-length bands "
            + ", ".join(sorted(bands))
            + ", and one ceiling does not cover them",
        )

    band = bands.pop()
    target = next((entry for entry in criteria.ttft if entry.band == band), None)
    if target is None:
        detail = (
            "the runs declared no input length, so no band applies"
            if band == buckets.UNKNOWN
            else f"{criteria.version} sets no time-to-first-token target for the {band} band"
        )
        return _absent(None, detail)

    threshold = target.p90_ms
    ordered = sorted(graded, key=lambda entry: entry.step_value or 0)

    crossed: Point | None = None
    before: Point | None = None
    previous: Point | None = None
    for entry in ordered:
        reading = entry.readings.get(KNEE_METRIC)
        value = reading.value if reading is not None else None
        if value is None:
            return _absent(
                threshold,
                f"the run at {step_field} {entry.step_value} reported no "
                f"{reading.path if reading and reading.path else KNEE_METRIC}, "
                "so the crossing cannot be located",
            )
        if value >= threshold:
            if previous is None:
                return _absent(
                    threshold,
                    f"the first step, at {step_field} {entry.step_value}, was already over "
                    f"{threshold} ms; the ramp started above the ceiling",
                )
            crossed = entry
            before = previous
            break
        previous = entry

    if crossed is None:
        top = ordered[-1]
        return _absent(
            threshold,
            f"every step stayed under {threshold} ms up to {step_field} {top.step_value}; "
            "the ramp did not reach the ceiling",
        )

    recrossed = _recrossing(ordered, crossed, threshold)
    if recrossed is not None:
        return _absent(
            threshold,
            f"{KNEE_METRIC} crossed {threshold} ms at {step_field} {crossed.step_value} and fell back under it at "
            f"{step_field} {recrossed.step_value}; the curve is not monotone and a first crossing read off it "
            "would be arbitrary",
        )

    return Knee(
        found=True,
        step_index=crossed.step_index,
        step_value=crossed.step_value,
        method=FIRST_CROSSING,
        metric=KNEE_METRIC,
        threshold=threshold,
        reason=(
            f"{KNEE_METRIC} crossed {threshold} ms between {step_field} "
            f"{before.step_value if before else None} and {step_field} {crossed.step_value}"
        ),
    )


def _point(run: Any, step_field: str) -> Point:
    parameters = getattr(run, "parameters", None) or {}
    phase = str(getattr(run, "phase", "") or "")
    adapter = summaries.adapter_for(run.harness, run.harness_version)
    summary = run.summary if phase == SUCCEEDED else None
    return Point(
        run_id=run.uid,
        step_index=int(getattr(run, "step_index", 0) or 0),
        step_value=_int(parameters.get(step_field)),
        input_tokens=_int(parameters.get(INPUT_TOKENS_MEAN)),
        serving_digest=getattr(run, "serving_digest", None) or None,
        phase=phase,
        discard=bool(getattr(run, "discard", False)),
        readings=_readings(adapter, summary),
    )


def _readings(adapter: summaries.Adapter | None, summary: dict | None) -> dict[str, Value]:
    """Every series of one summary, read through the harness's own field map.

    A run whose harness version no adapter was written for carries the keys with
    nothing in them and no path: the reading is not missing from the summary, it
    is a reading this plane cannot claim to know how to find.
    """
    values: dict[str, Value] = {}
    for series in SERIES:
        if adapter is None:
            values[series.key] = Value(None, "", "")
            continue
        field = next(
            (entry for entry in adapter.display + adapter.fields if entry.id == series.field_id),
            None,
        )
        if field is None:
            values[series.key] = Value(None, "", "")
            continue
        reading = summaries.show(adapter, summary, series.field_id)
        values[series.key] = Value(reading.value, field.unit, ".".join(field.path))
    return values


def _repeated_steps(graded: Sequence[Point]) -> tuple[int, int] | None:
    counts: dict[int, int] = {}
    for entry in graded:
        counts[entry.step_index] = counts.get(entry.step_index, 0) + 1
    return next(((index, count) for index, count in sorted(counts.items()) if count > 1), None)


def _recrossing(ordered: list[Point], crossed: Point, threshold: float) -> Point | None:
    after = ordered[ordered.index(crossed) + 1 :]
    for entry in after:
        reading = entry.readings.get(KNEE_METRIC)
        if reading is not None and reading.value is not None and reading.value < threshold:
            return entry
    return None


def _absent(threshold: float | None, reason: str) -> Knee:
    return Knee(
        found=False,
        step_index=None,
        step_value=None,
        method=FIRST_CROSSING,
        metric=KNEE_METRIC,
        threshold=threshold,
        reason=reason,
    )


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
