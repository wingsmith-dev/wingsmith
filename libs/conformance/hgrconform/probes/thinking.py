"""Thinking mode: whether it is on by default, how it is switched, and where the reasoning lands.

No two vendors spell any part of this the same way, so every request field comes
from `profile.thinking.body()` and every read comes from `profile.paths`. What the
probe contributes is the collapse: the reasoning text itself differs on every call
and is worthless to a diff, while the *place* it was delivered — a separate
`reasoning_content` field, `<think>` tags inside `content`, or nowhere at all — is
the thing the standard asks two endpoints to agree on.

Streaming is asked separately because it is a separate answer. An endpoint can
carry reasoning in its own field in a unary response and inline it in the deltas,
and a probe that only looked at one of the two would report agreement it has not
observed.
"""

from __future__ import annotations

import re

from typing import Any

from hgrconform.session import Session
from hgrconform.session import dig
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import probe


MAX_TOKENS = 256
THINK_TAG = re.compile(r"<think\b[^>]*>|</think>", re.IGNORECASE)
CONTENT_PATH = "choices.0.message.content"
DELTA_CONTENT_PATH = "choices.0.delta.content"


def _require_thinking(session: Session) -> None:
    if session.profile.thinking.syntax == "none":
        raise Skip("profile declares no thinking syntax")


def _request(session: Session, enabled: bool, effort: str = "") -> dict[str, Any]:
    """One request in this endpoint's thinking dialect, whichever kind of dialect it is.

    Two endpoints can carry the same switch in two different places: most take
    request fields, and some take a line of system prompt instead. Building the
    request through both halves means a probe never has to know which — and an
    endpoint of the second kind is not silently sent a parameter it ignores, which
    would return 200 and record it as conforming to something it never saw.
    """
    thinking = session.profile.thinking
    preamble = thinking.preamble(effort)
    body = session.body(max_tokens=MAX_TOKENS, **thinking.body(enabled, effort))
    if preamble:
        body["messages"] = [*preamble, *body["messages"]]
    return body


def _location(reasoning: Any, content: Any) -> str:
    """Where the reasoning was delivered, as one of four words.

    The words are the whole point. A diff over the reasoning text would fail on
    every run for a reason nobody cares about; a diff over "reasoning_content"
    against "inline-think-tags" is the finding the requirement is asking for.
    """
    separate = isinstance(reasoning, str) and bool(reasoning.strip())
    inline = isinstance(content, str) and bool(THINK_TAG.search(content))
    if separate and inline:
        return "both"
    if separate:
        return "reasoning_content"
    if inline:
        return "inline-think-tags"
    return "absent"


@probe(
    id="thinking.default",
    title="Whether a request carrying no thinking fields thinks anyway",
    requirement="Thinking Mode 1 — whether thinking mode is enabled by default, consistent with the provider",
    compare=("status", "reasoning_location", "thinking_by_default", "matches_profile_claim"),
)
def default(session: Session) -> dict[str, Any]:
    """A plain request, to see what the endpoint does when nobody asks.

    `matches_profile_claim` is the cross-check for a run with no reference
    endpoint: the profile says what the original provider does by default, and this
    says whether this endpoint did the same. It is `None` when the profile makes no
    claim, which compares cleanly as two endpoints both declining to answer.
    """
    _require_thinking(session)
    response = session.post("/v1/chat/completions", session.body(max_tokens=MAX_TOKENS))
    reasoning = response.at(session.profile.paths.reasoning)
    content = response.at(CONTENT_PATH)
    location = _location(reasoning, content)
    claimed = session.profile.thinking.default_on
    thought = location != "absent"
    return {
        "status": response.status,
        "reasoning_location": location,
        "thinking_by_default": thought,
        "profile_claims_default_on": claimed,
        "matches_profile_claim": None if claimed is None else thought == claimed,
        "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
        "message_fields": sorted(k for k in (dig(response.body, "choices.0.message") or {}) if not k.startswith("__")),
    }


@probe(
    id="thinking.toggle",
    title="Thinking explicitly enabled and explicitly disabled, in this profile's dialect",
    requirement="Thinking Mode 2 — the method for enabling and disabling thinking, and where thinking content lands",
    compare=(
        "syntax",
        "enabled_status",
        "enabled_location",
        "disabled_status",
        "disabled_location",
        "toggle_effective",
    ),
)
def toggle(session: Session) -> dict[str, Any]:
    """Both directions of the switch, because only the pair says the switch works.

    An endpoint that ignores the field entirely answers the two requests
    identically, and `toggle_effective` is that fact stated in one boolean rather
    than left for a reader to infer from two rows.
    """
    _require_thinking(session)
    thinking = session.profile.thinking

    on = session.post("/v1/chat/completions", _request(session, enabled=True))
    off = session.post("/v1/chat/completions", _request(session, enabled=False))

    reasoning_path = session.profile.paths.reasoning
    on_reasoning = on.at(reasoning_path)
    on_location = _location(on_reasoning, on.at(CONTENT_PATH))
    off_location = _location(off.at(reasoning_path), off.at(CONTENT_PATH))
    return {
        "syntax": thinking.syntax,
        "request_fields_enabled": sorted(thinking.body(True)),
        "enabled_status": on.status,
        "enabled_location": on_location,
        "disabled_status": off.status,
        "disabled_location": off_location,
        "toggle_effective": on_location != "absent" and off_location == "absent",
        "enabled_reasoning_chars": len(on_reasoning) if isinstance(on_reasoning, str) else 0,
    }


@probe(
    id="thinking.stream_location",
    title="Where reasoning is delivered in a stream with thinking enabled",
    requirement="Thinking Mode 2 — where the thinking content is returned, in the streaming case",
    compare=("status", "reasoning_location", "reasoning_before_content"),
)
def stream_location(session: Session) -> dict[str, Any]:
    """The same question asked of the deltas, which can answer it differently.

    `reasoning_before_content` records the ordering as a boolean instead of as the
    chunk index where reasoning stopped, for the reason `streaming.usage_location`
    records a position instead of an index: the index is a token count wearing a
    different hat.
    """
    _require_thinking(session)
    stream = session.stream(_request(session, enabled=True))

    delta_path = session.profile.paths.reasoning_delta
    reasoning_chunks = stream.chunks_with(delta_path)
    content_chunks = [i for i, c in enumerate(stream.chunks) if dig(c, DELTA_CONTENT_PATH)]
    joined = "".join(dig(c, delta_path) or "" for c in stream.chunks)
    content = "".join(dig(c, DELTA_CONTENT_PATH) or "" for c in stream.chunks)

    ordered = None
    if reasoning_chunks and content_chunks:
        ordered = max(reasoning_chunks) < min(content_chunks)
    return {
        "status": stream.status,
        "reasoning_location": _location(joined, content),
        "reasoning_in_any_delta": bool(reasoning_chunks),
        "reasoning_before_content": ordered,
        "chunk_count": len(stream.chunks),
        "reasoning_chunk_count": len(reasoning_chunks),
        "terminated": stream.done,
    }


@probe(
    id="thinking.efforts",
    title="Whether each effort level the profile declares is accepted",
    requirement="Thinking Mode 2 — the method for enabling thinking, at each effort level it offers",
    compare=("statuses", "levels", "all_accepted"),
)
def efforts(session: Session) -> dict[str, Any]:
    """One request per declared level, recorded as a status code and nothing else.

    A status code is a shape; the reasoning it produced is not. Whether a higher
    level thinks harder is a benchmark question and deliberately not asked here.
    """
    _require_thinking(session)
    levels = session.profile.thinking.efforts
    if not levels:
        raise Skip("profile declares no thinking efforts")

    statuses: dict[str, int] = {}
    locations: dict[str, str] = {}
    for level in levels:
        response = session.post(
            "/v1/chat/completions",
            _request(session, enabled=True, effort=level),
        )
        statuses[level] = response.status
        locations[level] = _location(response.at(session.profile.paths.reasoning), response.at(CONTENT_PATH))
    return {
        "levels": sorted(levels),
        "statuses": [f"{level}={statuses[level]}" for level in sorted(statuses)],
        "locations": [f"{level}={locations[level]}" for level in sorted(locations)],
        "all_accepted": all(200 <= status < 300 for status in statuses.values()),
    }
