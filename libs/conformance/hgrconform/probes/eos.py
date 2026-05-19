"""EOS suppression during reasoning, asked as a rate rather than as a single answer.

A reasoning model can emit the end-of-sequence token while it is still thinking. The
request then ends with `finish_reason: stop` and an empty `content`, which is a valid
response by the letter of the schema and useless by any other measure. Providers
suppress it. One request cannot see this, so the probe sends many and records how
often it happened.

The rate itself is not comparable — two runs of fifty samples never agree on a float
— so the compared key is the bucket the rate falls in. The raw rate, the counts and
the sample size stay in the observation as evidence.

Samples run concurrently, because serial execution of even the reduced default makes
this probe unusable. That is safe: `Session` locks the step that writes evidence,
which is the only part of it that threads contend on.
"""

from __future__ import annotations

import os

from typing import Any
from concurrent.futures import ThreadPoolExecutor

from hgrconform.session import Session
from hgrconform.session import dig
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import probe


SAMPLES = 50
WORKERS = 8
SAMPLES_ENV = "HGRCONFORM_EOS_SAMPLES"
WORKERS_ENV = "HGRCONFORM_EOS_WORKERS"

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


def _bucket(count: int, total: int) -> str:
    """The bucket a count falls in, kept apart from the case where nothing was sampled.

    Dividing by a floor of one turns "every request failed" into a rate of zero, and
    a rate of zero reads as "this endpoint never suppressed EOS" — a clean answer
    manufactured out of no evidence, indistinguishable in a diff from an endpoint
    that genuinely never did it. No successful sample is its own bucket.
    """
    if total <= 0:
        return "no-samples"
    rate = count / total
    if rate <= 0:
        return "none"
    if rate < 0.01:
        return "rare"
    if rate < 0.05:
        return "occasional"
    return "frequent"


@probe(
    id="eos.suppression",
    title="How often a reasoning request returns `finish_reason: stop` with empty content",
    requirement="II. Reasoning 4 — EOS emitted during the reasoning phase must be suppressed",
    compare=("empty_content_bucket", "empty_stop_bucket", "finish_reasons", "any_empty_content"),
    heavy=True,
)
def suppression(session: Session) -> dict[str, Any]:
    """The empty-response rate, bucketed, plus the finish reasons that came with it.

    Two things are counted apart. `empty_content` is any successful response with no
    text at all; `empty_stop` narrows that to the ones that also claimed `stop`, which
    is the shape the requirement describes — a truncation or a content filter arriving
    as an empty body is a different defect and says so through its own finish reason.
    """
    thinking = session.profile.thinking
    if thinking.syntax == "none" and not thinking.default_on:
        raise Skip("profile declares no way to request reasoning")

    samples = _positive_int(SAMPLES_ENV, SAMPLES)
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
        "any_empty_content": bool(empty_content),
        "finish_reasons": sorted(reasons),
        "finish_reason_counts": reasons,
        "statuses": sorted({r["status"] for r in results}),
    }
