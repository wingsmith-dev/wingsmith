"""Whether a run met the standard it was requested under, derived and never stored.

A verdict is a judgement *about* a run, not a phase *of* one. The run already
has a phase, written by ingestion from the frame that reported it, and a
`verdict` column beside it would be a second status with a second writer and no
frame behind it: an operation modelled as a status field, which is the first
rule.

Derived-at-read is only accurate if the standard is pinned, so the criteria are a
frozen table in code and the request records which entry it was created under.
A verdict is then a pure function of `(summary, harness, harness_version,
criteria)`: deterministic, re-derivable forever, and answerable for a version
other than its own: "would this run have passed under the new rules" is a
question a stored verdict cannot answer at all.

`inconclusive` is a first-class outcome and it is what a missing measurement
produces. A summary that lacks a field, a harness version outside the range any
adapter was written for, a dataset with no declared baseline, an accuracy run
whose coverage was heterogeneous: none of those are a pass.
"""

from typing import NamedTuple

from app.core.exceptions import Invalid

from . import buckets
from . import summaries


PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"
NOT_APPLICABLE = "not_applicable"

OUTCOMES = (PASS, FAIL, INCONCLUSIVE, NOT_APPLICABLE)

L1 = "L1"
L2 = "L2"
TIERS = (L1, L2)

LARGE_PARAMETER_COUNT = 10_000_000_000
"""The standard's own split between the two output-speed populations."""


class Check(NamedTuple):
    """One graded assertion, with the number it graded and the number it graded against."""

    id: str
    label: str
    outcome: str
    observed: float | None
    threshold: float | None
    comparator: str
    detail: str | None


class Verdict(NamedTuple):
    """The rollup of a run's checks under one criteria version.

    Unforgiving by design: any `fail` makes the verdict `fail`; otherwise any
    `inconclusive` makes it `inconclusive`; otherwise `pass`. A run with no
    applicable checks at all is `inconclusive`, because nothing was measured.
    """

    outcome: str
    criteria_version: str
    checks: tuple[Check, ...]
    reason: str | None


class TtftTarget(NamedTuple):
    """The three time-to-first-token ceilings one input band is held to, in milliseconds."""

    band: str
    p90_ms: float
    p50_ms: float
    avg_ms: float


class Criteria(NamedTuple):
    """One frozen revision of the acceptance standard.

    Entries are never edited, only superseded. Editing one would silently
    rewrite every verdict in history that sat near a line, with nothing on any
    row recording that it happened: worse than a stored status, because a
    stored status at least does not lie about the past.
    """

    version: str
    ttft: tuple[TtftTarget, ...]
    output_speed_large: dict[str, float]
    output_speed_small: float
    baseline_floor: float
    description: str


ACCEPTANCE_2026_1 = Criteria(
    version="acceptance-2026.1",
    description="The 2026.1 serving acceptance standard, transcribed as published.",
    ttft=(
        TtftTarget("lt6k", 5_000.0, 2_000.0, 2_000.0),
        # Transcribed as given. This row is internally odd: its average ceiling sits above its
        # P50 by 1.5s, implying a tail the P90 has already excluded. Copied faithfully,
        # not corrected: a frozen criteria entry is a record of what was published.
        TtftTarget("6k_16k", 5_000.0, 2_500.0, 4_000.0),
        TtftTarget("16k_32k", 8_000.0, 4_000.0, 6_000.0),
        TtftTarget("32k_64k", 15_000.0, 8_000.0, 8_000.0),
        TtftTarget("64k_128k", 35_000.0, 15_000.0, 15_000.0),
        TtftTarget("128k_256k", 70_000.0, 30_000.0, 30_000.0),
    ),
    output_speed_large={L1: 30.0, L2: 10.0},
    output_speed_small=100.0,
    baseline_floor=0.85,
)
"""The floor is `0.85 x baseline` and it lives here, not on the baseline row.

`ArtifactBaseline.tolerance_pct` is the per-row slack the recorder declared for that
dataset; the acceptance floor is a property of the standard. Burying the floor in the
column would let two artifacts be graded against two silently different standards, and
raising the standard would mean an UPDATE across every baseline ever recorded.
"""

CRITERIA: dict[str, Criteria] = {ACCEPTANCE_2026_1.version: ACCEPTANCE_2026_1}

DEFAULT_CRITERIA_VERSION = ACCEPTANCE_2026_1.version


def resolve_criteria(version: str | None) -> Criteria:
    if version is None:
        return CRITERIA[DEFAULT_CRITERIA_VERSION]
    if version not in CRITERIA:
        raise Invalid(f"unknown criteria version {version!r}", {"allowed": sorted(CRITERIA)})
    return CRITERIA[version]


class Baseline(NamedTuple):
    """One declared quality floor, flattened off `demand_artifact_baseline`.

    `value` and `tolerance_pct` are floats here and `Numeric` on the row, so the
    service converts once at the seam instead of leaving `Decimal` to meet a
    float threshold halfway through an expression.
    """

    dataset: str
    metric: str
    value: float
    tolerance_pct: float


class RunFacts(NamedTuple):
    """Everything a verdict needs about one run, read off the rows by the service.

    A record instead of eight arguments, and flat instead of the ORM objects,
    so that grading stays a pure function of values, which is what makes it
    re-derivable against a criteria version that did not exist when the run was
    taken.
    """

    run_id: str
    harness: str
    harness_version: str | None
    level: str
    phase: str
    discard: bool
    summary: dict | None
    input_tokens: int | None
    parameter_count: int | None
    tier: str | None
    strict: bool
    datasets: tuple[str, ...]
    baselines: tuple[Baseline, ...]
    homogeneous: bool


SUCCEEDED = "succeeded"


def verdict(facts: RunFacts, criteria: Criteria) -> Verdict:
    """Grades one run, or says why it cannot be graded."""
    if facts.discard:
        return Verdict(INCONCLUSIVE, criteria.version, (), "this run is marked discard and does not grade")
    if facts.phase != SUCCEEDED:
        return Verdict(INCONCLUSIVE, criteria.version, (), f"the run is {facts.phase} and reported no result")

    adapter = summaries.adapter_for(facts.harness, facts.harness_version)
    if adapter is None:
        return Verdict(
            INCONCLUSIVE,
            criteria.version,
            (),
            f"no field map for {facts.harness} at version {facts.harness_version or 'unreported'}",
        )

    if facts.harness == "conformance":
        checks = _conformance_checks(facts, adapter)
    elif facts.harness == "evalscope":
        checks = _accuracy_checks(facts, criteria)
    else:
        checks = _performance_checks(facts, adapter, criteria)

    return Verdict(rollup(checks), criteria.version, checks, None)


def rollup(checks: tuple[Check, ...]) -> str:
    """Any fail is a fail; otherwise any inconclusive; otherwise pass.

    `not_applicable` never carries the rollup on its own: a run whose every
    check was inapplicable measured nothing, and nothing is not a pass.
    """
    graded = [check for check in checks if check.outcome != NOT_APPLICABLE]
    if not graded:
        return INCONCLUSIVE
    if any(check.outcome == FAIL for check in graded):
        return FAIL
    if any(check.outcome == INCONCLUSIVE for check in graded):
        return INCONCLUSIVE
    return PASS


def rollup_verdicts(verdicts: list[Verdict]) -> str:
    """A request's outcome, which is its runs' outcomes under the same rule."""
    if not verdicts:
        return INCONCLUSIVE
    if any(entry.outcome == FAIL for entry in verdicts):
        return FAIL
    if any(entry.outcome == INCONCLUSIVE for entry in verdicts):
        return INCONCLUSIVE
    return PASS


# ----------------------------------------------------------------- performance


def _performance_checks(facts: RunFacts, adapter: summaries.Adapter, criteria: Criteria) -> tuple[Check, ...]:
    label = buckets.band_for(facts.input_tokens)
    target = next((entry for entry in criteria.ttft if entry.band == label), None)

    checks: list[Check] = []
    if target is None:
        detail = (
            "the run declared no input length, so no band applies"
            if label == buckets.UNKNOWN
            else f"{criteria.version} sets no time-to-first-token target above 256K"
        )
        outcome = INCONCLUSIVE if label == buckets.UNKNOWN else NOT_APPLICABLE
        checks.append(Check("ttft", f"time to first token ({label})", outcome, None, None, "<", detail))
    else:
        for field_id, ceiling in (
            (summaries.TTFT_P90, target.p90_ms),
            (summaries.TTFT_P50, target.p50_ms),
            (summaries.TTFT_AVG, target.avg_ms),
        ):
            checks.append(_below(adapter, facts.summary, field_id, f"{field_id} ({label})", ceiling))

    checks.append(_output_speed(facts, adapter, criteria))
    return tuple(checks)


def _below(adapter: summaries.Adapter, summary: dict | None, field_id: str, label: str, ceiling: float) -> Check:
    reading = summaries.read(adapter, summary, field_id)
    if reading.value is None:
        return Check(field_id, label, INCONCLUSIVE, None, ceiling, "<", f"{reading.missing} is not in the summary")
    outcome = PASS if reading.value < ceiling else FAIL
    return Check(field_id, label, outcome, reading.value, ceiling, "<", None)


def _output_speed(facts: RunFacts, adapter: summaries.Adapter, criteria: Criteria) -> Check:
    """Per-stream generation rate against the floor its population is held to.

    Per stream and never aggregate: 30 tokens per second aggregate on a serving
    GPU would itself be a fault, and the number only means anything as the rate
    one reader observes.

    The population comes from the artifact's parameter count. When that is null
    the check is `inconclusive` instead of defaulting to either branch: an
    unstated size must not silently take the lenient floor.
    """
    field_id = summaries.OUTPUT_SPEED_PER_STREAM
    label = "output speed per stream"

    if facts.parameter_count is None:
        return Check(field_id, label, INCONCLUSIVE, None, None, ">=", "the artifact declares no parameter count")

    if facts.parameter_count > LARGE_PARAMETER_COUNT:
        if facts.tier not in criteria.output_speed_large:
            return Check(
                field_id,
                label,
                INCONCLUSIVE,
                None,
                None,
                ">=",
                "a model above 10B parameters is graded per service tier and the request declared none",
            )
        floor = criteria.output_speed_large[facts.tier]
    else:
        floor = criteria.output_speed_small

    reading = summaries.read(adapter, facts.summary, field_id)
    if reading.value is None:
        return Check(field_id, label, INCONCLUSIVE, None, floor, ">=", f"{reading.missing} is not in the summary")
    outcome = PASS if reading.value >= floor else FAIL
    return Check(field_id, label, outcome, reading.value, floor, ">=", None)


# ----------------------------------------------------------------- accuracy


def _accuracy_checks(facts: RunFacts, criteria: Criteria) -> tuple[Check, ...]:
    """Each dataset's score against `0.85 x baseline`.

    A heterogeneous coverage is refused outright, not graded: two
    upstreams running different quantizations produce one score that belongs to
    neither, and a number nobody can attribute is worse than no number.

    A dataset with no baseline row is `inconclusive`, never a pass: the whole
    point of the baseline table is that an undeclared floor is an open question.
    A dataset that has a baseline but was not sat is `not_applicable`: a partial
    eval is a partial eval, and the request's own list says which exam was sat.
    """
    if not facts.homogeneous:
        return (
            Check(
                "accuracy",
                "accuracy against declared baselines",
                INCONCLUSIVE,
                None,
                None,
                ">=",
                "the request covered more than one artifact or serving configuration",
            ),
        )

    scored = summaries.dataset_scores(facts.summary)
    declared = {baseline.dataset: baseline for baseline in facts.baselines}
    wanted = list(facts.datasets) or sorted(set(scored) | set(declared))

    checks: list[Check] = []
    for dataset in wanted:
        check_id = f"accuracy.{dataset}"
        baseline = declared.get(dataset)
        if baseline is None:
            checks.append(
                Check(check_id, dataset, INCONCLUSIVE, None, None, ">=", "no baseline is declared for this dataset")
            )
            continue
        if dataset not in scored:
            floor = baseline.value * criteria.baseline_floor
            checks.append(
                Check(check_id, dataset, NOT_APPLICABLE, None, floor, ">=", "the run did not report this dataset")
            )
            continue
        metric, score = scored[dataset]
        floor = baseline.value * criteria.baseline_floor
        outcome = PASS if score >= floor else FAIL
        detail = None
        if metric != baseline.metric:
            outcome = INCONCLUSIVE
            detail = f"the run reports {metric!r} and the baseline declares {baseline.metric!r}"
        checks.append(Check(check_id, dataset, outcome, score, floor, ">=", detail))
    return tuple(checks)


# ----------------------------------------------------------------- conformance


def _conformance_checks(facts: RunFacts, adapter: summaries.Adapter) -> tuple[Check, ...]:
    """Every required probe must have passed, and the summary must agree with itself.

    A `counts.fail` above zero beside an empty failure list is `inconclusive`
    instead of a fail: the summary contradicted itself and neither half of it
    can be relied on to say what broke.
    """
    total = summaries.read(adapter, facts.summary, summaries.PROBE_TOTAL)
    failed = summaries.read(adapter, facts.summary, summaries.PROBE_FAILED)
    errored = summaries.read(adapter, facts.summary, summaries.PROBE_ERRORED)
    listed = summaries.failures(facts.summary)

    if total.value is None or failed.value is None:
        missing = total.missing or failed.missing
        return (
            Check("conformance", "probe suite", INCONCLUSIVE, None, None, "==", f"{missing} is not in the summary"),
        )

    if failed.value > 0 and not listed:
        return (
            Check(
                "conformance",
                "probe suite",
                INCONCLUSIVE,
                failed.value,
                0.0,
                "==",
                "the summary counts failures but enumerates none",
            ),
        )

    gating = [entry for entry in listed if facts.strict or entry.get("required") is True]
    advisory = len(listed) - len(gating)

    checks = [
        Check(
            "conformance.required",
            "required probes",
            FAIL if gating else PASS,
            float(len(gating)),
            0.0,
            "==",
            _first_failures(gating) if gating else None,
        )
    ]
    if advisory:
        checks.append(
            Check(
                "conformance.advisory",
                "advisory probes",
                NOT_APPLICABLE,
                float(advisory),
                None,
                "==",
                "advisory probes are reported and do not gate unless the run was strict",
            )
        )
    if errored.value:
        checks.append(
            Check(
                "conformance.errors",
                "probes that could not run",
                INCONCLUSIVE,
                errored.value,
                0.0,
                "==",
                "a probe that errored asserted nothing",
            )
        )
    return tuple(checks)


def _first_failures(entries: list[dict], limit: int = 5) -> str:
    named = [str(entry.get("id") or "unnamed") for entry in entries[:limit]]
    more = len(entries) - len(named)
    return ", ".join(named) + (f" and {more} more" if more > 0 else "")
