"""Prompt caching: whether it happens on its own, and where the count is reported.

Caching is invisible from the outside except through one number in `usage`, and
there is no agreed name for that number. Three spellings exist in the wild, so the
probe reads all three and records which one answered rather than concluding "no
cache" from the absence of the one this profile declared.

The question is asked with two calls that share a long, fixed prefix. Nothing here
is random: a cache hit depends on the prefix being byte-identical between the two
requests, so the filler is generated from a fixed sentence and a fixed count.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.session import dig
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import probe


KNOWN_PATHS = (
    "usage.prompt_cache_hit_tokens",
    "usage.cache_read_input_tokens",
)

FILLER_SENTENCE = (
    "The control plane declares the desired state and the cluster reports the observed state, "
    "and the two are never the same object. "
)

FILLER_REPEATS = 400


def _prefix() -> str:
    """A few thousand words of filler that is identical on every call and every run.

    A cache hit is a fact about two requests sharing a prefix byte for byte, so the
    filler must not carry a timestamp, a counter, or anything else that moves.
    """
    return FILLER_SENTENCE * FILLER_REPEATS


def _counts(response: Any, declared: str) -> dict[str, int | None]:
    paths = [declared, *[p for p in KNOWN_PATHS if p != declared]]
    return {path: dig(response.body, path) for path in paths}


def _answering_path(counts: dict[str, int | None]) -> str:
    for path, value in counts.items():
        if isinstance(value, int) and value > 0:
            return path
    for path, value in counts.items():
        if value is not None:
            return f"{path}:zero"
    return "absent"


@probe(
    id="cache.automatic",
    title="Whether a repeated prefix is cached without being asked for, and where the count appears",
    requirement="X. Cache Support 1 — whether caching is automatic or explicit; 3 — how cached tokens are retrieved",
    compare=(
        "cached_field_path",
        "cache_mode",
        "declared_path_resolves",
        "fields_present",
    ),
)
def automatic(session: Session) -> dict[str, Any]:
    """Two calls over one prefix, described by the shape of the answer rather than its size.

    A token count differs between two endpoints for reasons that have nothing to do
    with conformance — a different tokenizer, a different block size, a warm cache
    left by an earlier run. What the standard distinguishes is which field carries
    the answer, whether the endpoint cached without being asked, and whether the
    the count carried a value at all. Those are the comparable keys; the counts
    themselves, and whether the second call's count rose above the first's, are
    evidence below them — a cache left warm by an earlier run moves that comparison
    without telling us anything about the endpoint.
    """
    declared = session.profile.paths.cached_tokens
    if not declared:
        raise Skip("profile declares no cached_tokens path")

    prefix = _prefix()

    first = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[{"role": "user", "content": f"{prefix}\n\nAnswer with the single word: one."}],
            max_tokens=16,
            temperature=0,
        ),
    )
    second = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[{"role": "user", "content": f"{prefix}\n\nAnswer with the single word: two."}],
            max_tokens=16,
            temperature=0,
        ),
    )
    hinted = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[{"role": "user", "content": f"{prefix}\n\nAnswer with the single word: three."}],
            max_tokens=16,
            temperature=0,
            prompt_cache_key="hgrconform-cache-probe",
        ),
    )

    first_counts = _counts(first, declared)
    second_counts = _counts(second, declared)
    hinted_counts = _counts(hinted, declared)

    def best(counts: dict[str, int | None]) -> int:
        return max((v for v in counts.values() if isinstance(v, int)), default=0)

    automatic_hit = best(second_counts) > 0
    hinted_hit = best(hinted_counts) > 0
    if automatic_hit:
        mode = "automatic"
    elif hinted_hit:
        mode = "explicit"
    else:
        mode = "absent"

    usage = second.at("usage")
    return {
        "cached_field_path": _answering_path(second_counts),
        "cache_mode": mode,
        "cached_count_rose": best(second_counts) > best(first_counts),
        "declared_path_resolves": second_counts.get(declared) is not None,
        "fields_present": sorted(path for path, value in second_counts.items() if value is not None),
        "hint_accepted": hinted.ok,
        "statuses": [first.status, second.status, hinted.status],
        "usage_fields": sorted(k for k in usage if not k.startswith("__")) if isinstance(usage, dict) else [],
        "first_counts": first_counts,
        "second_counts": second_counts,
        "hinted_counts": hinted_counts,
        "prompt_tokens": [first.at("usage.prompt_tokens"), second.at("usage.prompt_tokens")],
        "prefix_chars": len(prefix),
    }
