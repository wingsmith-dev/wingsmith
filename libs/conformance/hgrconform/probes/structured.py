"""Structured output, and the one row in section I with a real pass or fail.

Almost everything else here is only meaningful against a reference endpoint. This
is the exception: when a request declares a JSON schema, the response either
validates against that schema or it does not, and no other endpoint's opinion is
needed. The observation still carries no verdict — but `schema_valid` is a fact
that means something on its own, which is rare in this package.
"""

from __future__ import annotations

import json

from typing import Any

import jsonschema

from hgrconform.session import Session
from hgrconform.probes.registry import probe


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "population": {"type": "integer"},
        "coastal": {"type": "boolean"},
        "districts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["city", "population", "coastal", "districts"],
    "additionalProperties": False,
}

ASK = "Describe Lisbon. Answer only with the JSON object."


@probe(
    id="structured.text",
    title='response_format {"type": "text"} is accepted',
    requirement='I. Structured Output Support 1 — response_format plain-text mode (type="text")',
    compare=("status", "has_content"),
)
def text(session: Session) -> dict[str, Any]:
    response = session.post(
        "/v1/chat/completions",
        session.body(max_tokens=64, response_format={"type": "text"}),
    )
    content = response.at("choices.0.message.content") or ""
    return {"status": response.status, "has_content": bool(content.strip())}


@probe(
    id="structured.json_object",
    title='response_format {"type": "json_object"} returns parseable JSON',
    requirement='I. Structured Output Support 2 — response_format JSON mode (type="json_object")',
    compare=("status", "parses"),
)
def json_object(session: Session) -> dict[str, Any]:
    response = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[{"role": "user", "content": f"{ASK} Use keys city and population."}],
            max_tokens=256,
            response_format={"type": "json_object"},
        ),
    )
    content = response.at("choices.0.message.content") or ""
    parses = True
    try:
        json.loads(content)
    except json.JSONDecodeError:
        parses = False
    return {"status": response.status, "parses": response.ok and parses}


@probe(
    id="structured.json_schema",
    title="A declared JSON schema is accepted, and the output conforms to it",
    requirement="I. Structured Output Support 3 — response_format structured output (type=json_schema)",
    compare=("status", "parses", "schema_valid", "strict_accepted"),
)
def json_schema(session: Session) -> dict[str, Any]:
    """Sends a schema exercising the keyword families the standard names, then validates.

    The schema covers the type keywords, a required list, an array with typed items,
    and `additionalProperties: false`, because a server that quietly drops the last
    one produces output that looks correct and is not.
    """
    request = session.body(
        messages=[{"role": "user", "content": ASK}],
        max_tokens=512,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "city", "strict": True, "schema": SCHEMA},
        },
    )
    response = session.post("/v1/chat/completions", request)
    content = response.at("choices.0.message.content") or ""

    parses, valid, error = False, False, ""
    try:
        parsed = json.loads(content)
        parses = True
        jsonschema.validate(parsed, SCHEMA)
        valid = True
    except json.JSONDecodeError as exc:
        error = f"not JSON: {exc}"
    except jsonschema.ValidationError as exc:
        error = f"schema violation at {'.'.join(str(p) for p in exc.absolute_path) or '<root>'}: {exc.message}"

    return {
        "status": response.status,
        "strict_accepted": response.ok,
        "parses": parses,
        "schema_valid": valid,
        "violation": error,
    }
