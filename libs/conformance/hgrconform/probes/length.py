"""Output length control, in its three common spellings.

`max_tokens`, `max_completion_tokens` and `max_output_tokens` are the same idea in
three API generations, and an endpoint can accept a parameter without honouring it.
Both halves are recorded: whether the request was accepted, and whether the output
actually stopped where it was told to.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import probe


LIMIT = 16
PROMPT = "Count slowly from one to two hundred, one number per line."


@probe(
    id="length.parameters",
    title="Which output-length parameters are accepted, and which are honoured",
    requirement="Output length control through max_tokens, max_completion_tokens and max_output_tokens",
    compare=("accepted", "honoured", "finish_reasons", "visible_content", "reasoning_only"),
)
def parameters(session: Session) -> dict[str, Any]:
    """Accepted and honoured are separate observations, because they differ in practice.

    An endpoint that ignores an unknown parameter returns 200 and a long answer; one
    that rejects it returns 400. Both are legitimate behaviours and the question is
    only whether this endpoint behaves as the original provider does, so both are
    recorded and neither is judged here.
    """
    accepted: dict[str, int] = {}
    honoured: dict[str, bool] = {}
    finish: dict[str, Any] = {}
    visible: dict[str, bool] = {}
    reasoning_only: dict[str, bool] = {}

    for parameter in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        body = session.body(messages=[{"role": "user", "content": PROMPT}], **{parameter: LIMIT})
        response = session.post("/v1/chat/completions", body)
        accepted[parameter] = response.status
        completion_tokens = response.at("usage.completion_tokens")
        honoured[parameter] = bool(response.ok and isinstance(completion_tokens, int) and completion_tokens <= LIMIT)
        finish[parameter] = response.at("choices.0.finish_reason")
        content = response.at("choices.0.message.content")
        reasoning = response.at(session.profile.paths.reasoning)
        visible[parameter] = bool(content)
        reasoning_only[parameter] = bool(response.ok and reasoning and not content)

    return {
        "accepted": accepted,
        "honoured": honoured,
        "finish_reasons": finish,
        "visible_content": visible,
        "reasoning_only": reasoning_only,
        "limit": LIMIT,
    }


@probe(
    id="length.context_overflow",
    title="What happens when the request exceeds the context window",
    requirement="What happens when a request exceeds the context window",
    compare=("status", "error_code", "error_type", "truncated"),
    requires=(),
)
def context_overflow(session: Session) -> dict[str, Any]:
    """Overshoots the declared window, and records how the endpoint says no.

    The profile's `context_window` is a claim, so the prompt is built from it and the
    observation includes what was actually sent. Where the profile makes no claim the
    probe skips instead of guessing, because a guess that happens to fit the window
    records a successful completion and looks like agreement.
    """
    window = session.profile.context_window
    if not window:
        raise Skip("profile declares no context_window")

    filler = "word " * int(window * 1.2)
    body = session.body(messages=[{"role": "user", "content": filler}], max_tokens=16)
    response = session.post("/v1/chat/completions", body)
    error = response.at("error") if isinstance(response.at("error"), dict) else {}

    return {
        "status": response.status,
        "declared_window": window,
        "approx_tokens_sent": int(window * 1.2),
        "error_code": error.get("code"),
        "error_type": error.get("type"),
        "truncated": bool(response.ok),
    }
