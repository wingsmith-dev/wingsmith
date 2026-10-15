"""Response correctness: the fields that must be there, and the ones that must not repeat.

Three probes, and the first is the one that needs the raw text. A response with a
duplicated field is invisible to any client that parses
JSON normally, because a repeated key is folded into one on the way in. The session
keeps the duplicates; this reads them back.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.session import duplicate_keys
from hgrconform.probes.registry import probe


@probe(
    id="basics.completion",
    title="A minimal chat completion returns the required fields, non-empty",
    requirement="A completion returns its key fields, populated, with no duplicated JSON keys",
    compare=("status", "finish_reason", "has_content", "usage_fields", "duplicate_keys", "top_level_fields"),
)
def completion(session: Session) -> dict[str, Any]:
    response = session.post("/v1/chat/completions", session.body(max_tokens=32))
    body = response.body

    content = response.at("choices.0.message.content")
    usage = response.at("usage") or {}
    observed: dict[str, Any] = {
        "status": response.status,
        "finish_reason": response.at("choices.0.finish_reason"),
        "has_content": bool(content and content.strip()),
        "role": response.at("choices.0.message.role"),
        "usage_fields": sorted(k for k in usage if not k.startswith("__")) if isinstance(usage, dict) else [],
        "duplicate_keys": duplicate_keys(body),
        "object": response.at("object"),
    }
    if isinstance(body, dict):
        observed["top_level_fields"] = sorted(k for k in body if not k.startswith("__"))
    return observed


@probe(
    id="basics.multi_turn",
    title="A three-turn conversation stays coherent and terminates normally",
    requirement="Earlier turns are carried: no empty or garbled output, and a correct finish_reason",
    compare=("status", "finish_reason", "has_content", "recalled"),
)
def multi_turn(session: Session) -> dict[str, Any]:
    """Whether the endpoint carries earlier turns, checked with a fact only the history holds.

    The recall test is deliberately trivial — a number stated in turn one and asked
    for in turn three. Anything harder starts measuring the model rather than the
    endpoint, and the endpoint is what this package is about.
    """
    messages = [
        {"role": "user", "content": "Remember the number 4173. Reply with OK."},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "What is 2 plus 2? Reply with the digit only."},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "What number did I ask you to remember? Reply with the digits only."},
    ]
    response = session.post("/v1/chat/completions", session.body(messages=messages, max_tokens=32))
    content = response.at("choices.0.message.content") or ""
    return {
        "status": response.status,
        "finish_reason": response.at("choices.0.finish_reason"),
        "has_content": bool(content.strip()),
        "recalled": "4173" in content,
        "turns": len(messages),
    }


@probe(
    id="basics.models",
    title="The model list, and whether the model under test is in it",
    requirement="/v1/models lists the served model, and what it claims about it",
    compare=("status", "model_listed", "model_fields"),
)
def models(session: Session) -> dict[str, Any]:
    response = session.get("/v1/models")
    entries = response.at("data") or []
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    listed = next((e for e in entries if isinstance(e, dict) and e.get("id") == session.profile.model), None)
    return {
        "status": response.status,
        "model_listed": session.profile.model in ids,
        "model_count": len(ids),
        "model_fields": sorted(k for k in listed if not k.startswith("__")) if listed else [],
    }
