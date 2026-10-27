"""Which of the three request protocols the endpoint answers.

The three are the OpenAI Chat Completions API, the OpenAI Responses API and the
Anthropic Messages API, each answered the way the original model provider answers it.
Not answering one is a legitimate result (most open-source servers answer only the
first), so what is recorded is the status code, and the diff decides whether that
matches what the provider does.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.probes.registry import probe


@probe(
    id="protocol.surfaces",
    title="Status codes for /v1/chat/completions, /v1/responses and /v1/messages",
    requirement="The three API surfaces: OpenAI Chat Completions, OpenAI Responses, Anthropic Messages",
    compare=("chat_completions", "responses", "messages", "answers"),
)
def surfaces(session: Session) -> dict[str, Any]:
    """One well-formed request per protocol, in that protocol's own body shape.

    Sending a chat-completions body to all three would tell us only that two of them
    reject malformed input. Each request here is the minimal valid one for its own
    API, so a 404 means the route is absent and a 400 means the route exists and
    disagreed with us: a distinction a caller cares about.
    """
    chat = session.post("/v1/chat/completions", session.body(max_tokens=8))

    responses = session.post(
        "/v1/responses",
        {"model": session.profile.model, "input": "Say OK and nothing else.", "max_output_tokens": 16},
    )

    messages = session.post(
        "/v1/messages",
        {
            "model": session.profile.model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Say OK and nothing else."}],
        },
    )

    answers = [
        name
        for name, response in (
            ("chat_completions", chat),
            ("responses", responses),
            ("messages", messages),
        )
        if response.ok
    ]

    return {
        "chat_completions": chat.status,
        "responses": responses.status,
        "messages": messages.status,
        "answers": answers,
    }
