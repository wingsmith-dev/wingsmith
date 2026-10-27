"""Usage accounting: whether `usage` comes back at all, and what it counts the prompt as.

The vendor verifier this replaces asserted `prompt_tokens` against a constant written
down per case. A constant is a claim about one tokenizer at one moment, and it is wrong
the day the model is retrained. The question worth asking is parity: two endpoints
serving the same model must tokenize the same bytes into the same number of tokens, so
the counts themselves are the comparable value here, which is unusual: a difference is exactly
the defect and needs no threshold to interpret.

The corpus is fixed and lives in this file so a change to what is asked is a change
visible in a diff. Each case exercises a shape that tokenizers and template renderers
disagree about: the chat template's own overhead, a system turn, a multi-turn history,
non-BMP text, code fences, and a long repeat where an off-by-one per token compounds
into a number nobody can mistake for noise.

`total == prompt + completion` is checked per case because an endpoint that mis-sums its
own usage object is a finding on its own, visible without any reference to diff against.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import probe


CASES: dict[str, list[dict[str, Any]]] = {
    "bare": [
        {"role": "user", "content": "Say OK."},
    ],
    "system": [
        {"role": "system", "content": "You are a terse assistant. Answer in one word."},
        {"role": "user", "content": "Say OK."},
    ],
    "multi_turn": [
        {"role": "user", "content": "What is two plus two?"},
        {"role": "assistant", "content": "Four."},
        {"role": "user", "content": "And plus one more?"},
    ],
    "unicode": [
        {"role": "user", "content": "Repeat this exactly: héllo wörld \u2014 你好世界 🚀🇧🇷 café naïve"},
    ],
    "code_fence": [
        {
            "role": "user",
            "content": "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```\nSay OK.",
        },
    ],
    "long_repeat": [
        {"role": "user", "content": ("the quick brown fox jumps over the lazy dog. " * 200) + "Say OK."},
    ],
}


@probe(
    id="tokens.usage",
    title="`usage` is present and populated on a non-streaming response",
    requirement="Usage is returned and populated",
    compare=("status", "usage_present", "usage_fields", "counts_positive"),
)
def usage(session: Session) -> dict[str, Any]:
    """Whether the usage object exists and carries numbers, not zeros.

    `counts_positive` is the shape of the answer and not the counts: an endpoint that
    returns the field with every value at zero has satisfied the schema and answered
    nothing, and that is the failure this row is for.

    `stream` is pinned off, not left out, because an endpoint that streams by
    default would answer a different question: the location of usage in a stream is
    `streaming.usage_location`, and this row is about the non-streaming body.
    """
    if "chat_completions" not in session.profile.protocols:
        raise Skip("profile declares no chat_completions protocol")

    response = session.post(
        "/v1/chat/completions",
        session.body(max_tokens=16, stream=False),
    )
    reported = response.at("usage")
    fields = sorted(k for k in reported if not k.startswith("__")) if isinstance(reported, dict) else []
    counts = {name: response.at(f"usage.{name}") for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return {
        "status": response.status,
        "usage_present": isinstance(reported, dict),
        "usage_fields": fields,
        "counts_positive": {name: (isinstance(value, int) and value > 0) for name, value in counts.items()},
        "counts": counts,
        "cached_tokens": response.at(session.profile.paths.cached_tokens),
    }


@probe(
    id="tokens.prompt_accounting",
    title="Reported `prompt_tokens` for a fixed corpus of prompt shapes",
    requirement="Usage is returned and populated, and prompt tokens are counted as the original provider counts them",
    compare=("prompt_tokens", "total_is_sum", "statuses"),
)
def prompt_accounting(session: Session) -> dict[str, Any]:
    """The raw prompt counts, which are comparable here because the input is fixed.

    Nothing generated goes into the comparison: the completion is capped at one token
    and its count is evidence only, because the model is free to stop where it likes and
    a difference there is not a defect. The prompt is bytes we chose, so a difference
    there is one.

    The corpus is walked serially. The session appends to a shared list and names its
    evidence files from that list's length, so a thread per case would overwrite the
    files it was sent to collect; six requests are not worth a lock.
    """
    if "chat_completions" not in session.profile.protocols:
        raise Skip("profile declares no chat_completions protocol")

    prompt_tokens: dict[str, Any] = {}
    completion_tokens: dict[str, Any] = {}
    total_tokens: dict[str, Any] = {}
    total_is_sum: dict[str, Any] = {}
    statuses: dict[str, int] = {}

    for name, messages in CASES.items():
        response = session.post(
            "/v1/chat/completions",
            session.body(messages=messages, max_tokens=1, stream=False),
        )
        statuses[name] = response.status
        prompt = response.at("usage.prompt_tokens")
        completion = response.at("usage.completion_tokens")
        total = response.at("usage.total_tokens")
        prompt_tokens[name] = prompt
        completion_tokens[name] = completion
        total_tokens[name] = total
        total_is_sum[name] = (
            total == prompt + completion
            if all(isinstance(value, int) for value in (prompt, completion, total))
            else None
        )

    return {
        "prompt_tokens": prompt_tokens,
        "total_is_sum": total_is_sum,
        "statuses": statuses,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cases": sorted(CASES),
    }
