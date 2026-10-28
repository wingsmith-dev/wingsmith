"""Streaming: whether it is on by default, how it is turned on, and where `usage` lands.

The third of these is the interesting one and the reason the session keeps chunks
unmerged. What is checked is whether the *location* of the usage field (every chunk,
or only the last one) matches the original provider, and that is a question about
the shape of the stream, not about its contents. A client that accumulates
deltas as they arrive has thrown the answer away before it can be asked.
"""

from __future__ import annotations

from typing import Any

from wingsconform.session import Session
from wingsconform.session import dig
from wingsconform.probes.registry import probe


USAGE_PATH = "usage.total_tokens"


@probe(
    id="streaming.default",
    title="Whether a request without `stream` is streamed",
    requirement="Whether streaming is on by default",
    compare=("content_type", "streamed_by_default"),
)
def default(session: Session) -> dict[str, Any]:
    response = session.post("/v1/chat/completions", session.body(max_tokens=16))
    content_type = response.headers.get("content-type", "").split(";")[0]
    return {
        "status": response.status,
        "content_type": content_type,
        "streamed_by_default": content_type == "text/event-stream",
        "object": response.at("object"),
    }


@probe(
    id="streaming.toggle",
    title="`stream: true` produces an SSE stream that terminates with [DONE]",
    requirement="How streaming is switched on and off",
    compare=("status", "content_type", "terminated", "has_content", "first_delta_fields"),
)
def toggle(session: Session) -> dict[str, Any]:
    stream = session.stream(session.body(max_tokens=32))
    first = stream.chunks[0] if stream.chunks else None
    delta = dig(first, "choices.0.delta") or {}
    return {
        "status": stream.status,
        "content_type": stream.headers.get("content-type", "").split(";")[0],
        "terminated": stream.done,
        "chunk_count": len(stream.chunks),
        "has_content": bool(stream.text().strip()),
        "first_delta_fields": sorted(k for k in delta if not k.startswith("__")) if isinstance(delta, dict) else [],
        "finish_reason": next(
            (dig(c, "choices.0.finish_reason") for c in reversed(stream.chunks) if dig(c, "choices.0.finish_reason")),
            None,
        ),
    }


@probe(
    id="streaming.usage_location",
    title="Which chunks carry `usage`, with and without `stream_options.include_usage`",
    requirement="Token usage in a stream: whether it is sent, and in which chunk",
    compare=(
        "usage_without_option",
        "usage_with_option",
        "usage_position",
        "usage_fields",
        "include_usage_accepted",
    ),
)
def usage_location(session: Session) -> dict[str, Any]:
    """Where the usage object appears, described as a position instead of a chunk index.

    An index would differ between two endpoints for an uninteresting reason: they
    generated a different number of tokens. `usage_position` collapses that to the
    three answers that actually differ for a client: nowhere, in the final chunk
    only, or in every chunk.
    """
    plain = session.stream(session.body(max_tokens=32))
    plain_carrying = plain.chunks_with(USAGE_PATH)

    opted = session.stream(session.body(max_tokens=32, stream_options={"include_usage": True}))
    opted_carrying = opted.chunks_with(USAGE_PATH)

    def position(carrying: list[int], total: int) -> str:
        if not carrying:
            return "absent"
        if len(carrying) == total:
            return "every-chunk"
        if carrying == [total - 1]:
            return "final-chunk"
        return "some-chunks"

    usage = next((dig(c, "usage") for c in reversed(opted.chunks) if dig(c, "usage")), None)
    return {
        "usage_without_option": bool(plain_carrying),
        "usage_with_option": bool(opted_carrying),
        "usage_position": position(opted_carrying, len(opted.chunks)),
        "usage_position_without_option": position(plain_carrying, len(plain.chunks)),
        "include_usage_accepted": opted.ok,
        "usage_fields": sorted(k for k in usage if not k.startswith("__")) if isinstance(usage, dict) else [],
    }
