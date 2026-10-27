"""Input-length bands, and how a request's runs are read back through them.

The acceptance standard sets a separate time-to-first-token target for each band
of prompt length, so one aggregate number over a whole sweep evidences nothing:
a request that averaged 4s of TTFT may have been comfortably inside the target
at 2K tokens and far outside it at 100K, and the average says neither. One
AIPerf invocation measures one load point, so a swept request is N runs and the
re-aggregation is a group-by over its own children.

**The boundaries are literals here and are intentionally duplicated.** They belong
to a frozen criteria version and to the gateway's Go constant, and to nothing
else. A shared constant in the metric registry, editable by anybody adding a
metric, would let an edit silently rewrite what a two-year-old verdict meant.
The eight label strings are the contract; the numbers are their definition.

K is 1024 throughout, the way a context window is quoted.
"""

from typing import Any
from typing import NamedTuple

from . import summaries


UNKNOWN = "unknown"
"""No prompt length was declared, so no band can be claimed.

Its own value instead of a silent drop, for the same reason the gateway has
one: a coverage gap belongs on the panel, not inside the number.
"""


class Band(NamedTuple):
    """One input-length band, named by the label the whole system spells it with.

    `upper` is inclusive and `None` is unbounded. Inclusive because a run whose
    prompt length lands exactly on a boundary takes the *stricter* band, and the
    stricter of two adjacent bands is always the lower one.
    """

    label: str
    upper: int | None
    description: str


BANDS: tuple[Band, ...] = (
    Band("lt6k", 6 * 1024, "under 6K input tokens"),
    Band("6k_16k", 16 * 1024, "6K to 16K input tokens"),
    Band("16k_32k", 32 * 1024, "16K to 32K input tokens"),
    Band("32k_64k", 64 * 1024, "32K to 64K input tokens"),
    Band("64k_128k", 128 * 1024, "64K to 128K input tokens"),
    Band("128k_256k", 256 * 1024, "128K to 256K input tokens"),
    Band("gt256k", None, "over 256K input tokens"),
)

LABELS: tuple[str, ...] = tuple(band.label for band in BANDS) + (UNKNOWN,)


class Sample(NamedTuple):
    """One run, reduced to what a band report needs of it."""

    run_id: str
    harness: str
    harness_version: str | None
    input_tokens: int | None
    discard: bool
    summary: dict | None


class BandReport(NamedTuple):
    """Every run of one request that landed in one band, read back together.

    `p90` and `p50` are the worst of the band instead of a mean of percentiles:
    percentiles do not average, and a claim about a band has to hold for every
    load point inside it. `avg` is weighted by each run's request count, which
    is the one of the three that genuinely combines.

    `incomplete` names the runs whose summary carried no TTFT at all, or an average with no
    request count to weight it by. They are listed, not dropped, so a band that measured
    less than it claims says so.
    """

    band: str
    runs: int
    requests: int
    ttft_p90_ms: float | None
    ttft_p50_ms: float | None
    ttft_avg_ms: float | None
    incomplete: tuple[str, ...]


def band_for(input_tokens: int | None) -> str:
    """The band a prompt length falls in, with a boundary taking the stricter side."""
    if input_tokens is None or input_tokens < 0:
        return UNKNOWN
    for band in BANDS:
        if band.upper is None or input_tokens <= band.upper:
            return band.label
    return UNKNOWN


def band(label: str) -> Band | None:
    return next((entry for entry in BANDS if entry.label == label), None)


def sample(run: Any) -> Sample:
    """One run row reduced to a sample, reading the prompt length off its parameters.

    The prompt length comes from the run's own declared `input_tokens_mean` and
    never from the summary. The summary is verbatim and is under no obligation
    to name a band, or to name it the same way twice.
    """
    parameters = getattr(run, "parameters", None) or {}
    declared = parameters.get("input_tokens_mean")
    return Sample(
        run_id=run.uid,
        harness=run.harness,
        harness_version=run.harness_version,
        input_tokens=int(declared) if isinstance(declared, int) and not isinstance(declared, bool) else None,
        discard=bool(getattr(run, "discard", False)),
        summary=run.summary,
    )


def reaggregate(samples: list[Sample]) -> list[BandReport]:
    """Groups a request's runs into bands, newest boundaries first.

    Discarded runs (a `warmup` mode's single low-concurrency pass) are excluded
    outright, not reported empty. They were never a measurement, and a
    band whose only member is a warm-up would otherwise claim one.

    Bands nothing landed in are absent from the result. A report listing six
    empty bands beside one populated one reads as six failures.
    """
    grouped: dict[str, list[Sample]] = {}
    for entry in samples:
        if entry.discard:
            continue
        grouped.setdefault(band_for(entry.input_tokens), []).append(entry)

    reports: list[BandReport] = []
    for label in LABELS:
        members = grouped.get(label)
        if not members:
            continue
        reports.append(_report(label, members))
    return reports


def _report(label: str, members: list[Sample]) -> BandReport:
    worst_p90: float | None = None
    worst_p50: float | None = None
    weighted = 0.0
    weight = 0.0
    requests = 0
    incomplete: list[str] = []

    for entry in members:
        adapter = summaries.adapter_for(entry.harness, entry.harness_version)
        if adapter is None:
            incomplete.append(entry.run_id)
            continue
        p90 = summaries.read(adapter, entry.summary, summaries.TTFT_P90)
        p50 = summaries.read(adapter, entry.summary, summaries.TTFT_P50)
        avg = summaries.read(adapter, entry.summary, summaries.TTFT_AVG)
        count = summaries.read(adapter, entry.summary, summaries.REQUEST_COUNT)
        if p90.value is None and p50.value is None and avg.value is None:
            incomplete.append(entry.run_id)
            continue
        if p90.value is not None:
            worst_p90 = p90.value if worst_p90 is None else max(worst_p90, p90.value)
        if p50.value is not None:
            worst_p50 = p50.value if worst_p50 is None else max(worst_p50, p50.value)
        if avg.value is not None:
            if count.value:
                weighted += avg.value * count.value
                weight += count.value
            else:
                # An average with no request count has no weight; guessing one would skew the band.
                incomplete.append(entry.run_id)
        if count.value:
            requests += int(count.value)

    return BandReport(
        band=label,
        runs=len(members),
        requests=requests,
        ttft_p90_ms=worst_p90,
        ttft_p50_ms=worst_p50,
        ttft_avg_ms=(weighted / weight) if weight else None,
        incomplete=tuple(incomplete),
    )
