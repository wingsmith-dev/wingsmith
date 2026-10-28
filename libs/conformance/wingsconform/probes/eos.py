"""EOS suppression during reasoning, asked as a rate, not as a single answer.

A reasoning model can emit the end-of-sequence token while it is still thinking. The
request then ends with `finish_reason: stop` and an empty `content`, which is a valid
response by the letter of the schema and useless by any other measure. Providers
suppress it. One request cannot see this, so the probe sends many and records how
often it happened.

The rate itself is not comparable, since two runs of fifty samples never agree on a
float. The compared key is the bucket the rate falls in. The raw rate, the counts, the
finish reasons and the sample size stay in the observation as evidence.

The buckets are only as fine as the sample allows, and no coarser than one sample
must never cross. Zero is not a bucket of its own: an endpoint that suppresses EOS
well still lets one through now and then, and "never in this run" against "once in
this run" is a coin toss between two conforming endpoints, not a difference between
them. The lowest bucket therefore runs from zero up to `RARE_BELOW`, and
`MIN_SAMPLES` keeps `1/n` at or under half of that line, so it takes at least two
empty samples to leave it. Every bucket stays reachable at that size, and a smaller
override is raised to it, not honoured.

Samples run concurrently, because serial execution of even the reduced default makes
this probe unusable. That is safe: `Session` locks the step that writes evidence,
which is the only part of it that threads contend on.
"""

from __future__ import annotations

import os

from typing import Any
from concurrent.futures import ThreadPoolExecutor

from wingsconform.session import Session
from wingsconform.session import dig
from wingsconform.probes.registry import Skip
from wingsconform.probes.registry import probe


SAMPLES = 50
WORKERS = 8
# Upper bounds of the buckets. `rare` includes zero. MIN_SAMPLES puts 1/n at half of
# RARE_BELOW, so one empty sample on its own never leaves `rare`.
RARE_BELOW = 0.05
OCCASIONAL_BELOW = 0.20
MIN_SAMPLES = 40
SAMPLES_ENV = "WINGSCONFORM_EOS_SAMPLES"
WORKERS_ENV = "WINGSCONFORM_EOS_WORKERS"

PROMPT = (
    "A train leaves station A at 09:00 travelling at 60 km/h. A second train leaves station B, "
    "420 km away, at 09:30 travelling toward A at 90 km/h. Work out when and where they meet, "
    "then check the answer a second way and say whether the two methods agree."
)


def _positive_int(name: str, default: int) -> int:
    """An override read from the environment, ignored when it is not a positive number."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def sample_size() -> int:
    """The number of samples to send: the override if any, never below MIN_SAMPLES."""
    return max(MIN_SAMPLES, _positive_int(SAMPLES_ENV, SAMPLES))


def _bucket(count: int, total: int) -> str:
    """The bucket a count falls in, kept apart from the case where nothing was sampled.

    Dividing by a floor of one turns "every request failed" into a rate of zero, and
    a rate of zero reads as "this endpoint never suppressed EOS": a clean answer
    manufactured out of no evidence, indistinguishable in a diff from an endpoint
    that genuinely never did it. No successful sample is its own bucket.

    A rate of zero is not: it shares `rare` with the rates one stray sample produces.
    """
    if total <= 0:
        return "no-samples"
    rate = count / total
    if rate < RARE_BELOW:
        return "rare"
    if rate < OCCASIONAL_BELOW:
        return "occasional"
    return "frequent"


@probe(
    id="eos.suppression",
    title="How often a reasoning request returns `finish_reason: stop` with empty content",
    requirement="An end-of-sequence token emitted while reasoning is suppressed instead of ending the response empty",
    compare=("empty_content_bucket", "empty_stop_bucket"),
    heavy=True,
)
def suppression(session: Session) -> dict[str, Any]:
    """The empty-response rate, bucketed, plus the finish reasons that came with it.

    Two things are counted apart. `empty_content` is any successful response with no
    text at all; `empty_stop` narrows that to the ones that also claimed `stop`, which
    is the shape the requirement describes: a truncation or a content filter arriving
    as an empty body is a different defect and says so through its own finish reason.

    Only the buckets are compared, and neither of them separates none from one.
    Whether any sample at all came back empty, and the set of finish reasons seen,
    both turn on a single rare event in a run of fifty, so two conforming endpoints
    disagree on them by chance; they stay in the observation as evidence.
    """
    thinking = session.profile.thinking
    if thinking.syntax == "none" and not thinking.default_on:
        raise Skip("profile declares no way to request reasoning")

    samples = sample_size()
    workers = _positive_int(WORKERS_ENV, WORKERS)
    body = session.body(messages=[{"role": "user", "content": PROMPT}], **thinking.body(True))

    def one(_: int) -> dict[str, Any]:
        response = session.post("/v1/chat/completions", body)
        content = str(response.at("choices.0.message.content") or "")
        reasoning = str(dig(response.body, session.profile.paths.reasoning) or "")
        return {
            "status": response.status,
            "finish_reason": response.at("choices.0.finish_reason"),
            "empty": not content.strip(),
            "reasoning": bool(reasoning.strip()),
        }

    with ThreadPoolExecutor(max_workers=min(workers, samples)) as pool:
        results = list(pool.map(one, range(samples)))

    ok = [r for r in results if 200 <= r["status"] < 300]
    reasons: dict[str, int] = {}
    for result in ok:
        key = str(result["finish_reason"])
        reasons[key] = reasons.get(key, 0) + 1

    empty_content = [r for r in ok if r["empty"]]
    empty_stop = [r for r in empty_content if r["finish_reason"] == "stop"]
    denominator = len(ok)

    return {
        "samples_requested": samples,
        "samples_ok": len(ok),
        "workers": workers,
        "empty_content_count": len(empty_content),
        "empty_stop_count": len(empty_stop),
        "empty_with_reasoning_count": sum(1 for r in empty_content if r["reasoning"]),
        "reasoning_present_count": sum(1 for r in ok if r["reasoning"]),
        "empty_content_rate": round(len(empty_content) / denominator, 4) if denominator else None,
        "empty_stop_rate": round(len(empty_stop) / denominator, 4) if denominator else None,
        "empty_content_bucket": _bucket(len(empty_content), denominator),
        "empty_stop_bucket": _bucket(len(empty_stop), denominator),
        "smallest_observable_rate": round(1 / denominator, 4) if denominator else None,
        "any_empty_content": bool(empty_content),
        "finish_reasons": sorted(reasons),
        "finish_reason_counts": reasons,
        "statuses": sorted({r["status"] for r in results}),
    }
