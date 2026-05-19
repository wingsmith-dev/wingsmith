"""Tool calling: the protocol, the round trip, and the two ways it goes wrong.

Four rows of the standard land here, and they are not the same kind of question.
Protocol and round trip are mechanical — either the endpoint accepts the four
`tool_choice` modes and reassembles a streamed call correctly, or it does not.
Parallel calls and hallucination are behavioural, and behaviour varies between two
honest runs of the same model.

That difference decides what may be compared. `tool_choice: required` producing no
call at all is a protocol defect and is comparable. *Which* of two equally valid
tools the model picked for an ambiguous prompt is not, and appears only as evidence.
The prompts here are therefore written to be as unambiguous as a prompt can be, so
that what remains in `compare` is close to deterministic.

What is never compared is the argument text. Two endpoints serving one model will
phrase `{"city": "Lisbon"}` and `{"city":"Lisbon"}` differently and mean the same
thing; what matters is that it parsed and that it satisfied the schema we sent.
"""

from __future__ import annotations

import json

from typing import Any

import jsonschema

from hgrconform.session import Session
from hgrconform.session import dig
from hgrconform.probes.registry import probe


WEATHER: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather in a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "The city name"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}

CLOCK: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current time in a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}

ASK_WEATHER = "What is the weather in Lisbon? Use the tool."
ASK_BOTH = "What is the weather in Lisbon, and what time is it in Tokyo? Use the tools."
SMALL_TALK = "Say the word hello. Do not use any tool."


def _calls(response: Any) -> list[dict[str, Any]]:
    raw = dig(response, "choices.0.message.tool_calls") or []
    return [call for call in raw if isinstance(call, dict)]


def _arguments(call: dict[str, Any]) -> tuple[bool, bool, str]:
    """Whether one call's arguments parse, and whether they satisfy the schema we sent.

    Returned as two booleans rather than the argument string, because the string is
    the least comparable thing in the whole response and the two booleans are the
    whole question.
    """
    text = dig(call, "function.arguments")
    if not isinstance(text, str):
        return False, False, "arguments is not a string"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, False, f"not JSON: {exc}"

    name = dig(call, "function.name")
    schema = next(
        (tool["function"]["parameters"] for tool in (WEATHER, CLOCK) if tool["function"]["name"] == name),
        None,
    )
    if schema is None:
        return True, False, f"call names an undeclared function {name!r}"
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        return True, False, f"schema violation: {exc.message}"
    return True, True, ""


@probe(
    id="tools.protocol",
    title="The four tool_choice modes, and the structure of a returned call",
    requirement="I. Tool Calling — protocol structure, four tool_choice modes",
    compare=("statuses", "called", "finish_reasons", "call_fields", "arguments_valid"),
)
def protocol(session: Session) -> dict[str, Any]:
    """Each mode is a separate contract, and an endpoint can honour some and not others.

    `called` is a boolean per mode rather than a count, because the contract is about
    whether a call happened at all: `none` must not call, `required` and a named
    choice must, and `auto` is the model's decision and is recorded but weakly
    comparable.
    """
    modes: dict[str, Any] = {
        "auto": "auto",
        "none": "none",
        "required": "required",
        "named": {"type": "function", "function": {"name": "get_weather"}},
    }

    statuses: dict[str, int] = {}
    called: dict[str, bool] = {}
    names: dict[str, list[str]] = {}
    finish: dict[str, Any] = {}
    valid: dict[str, bool] = {}
    problems: dict[str, str] = {}
    fields: list[str] = []

    for mode, value in modes.items():
        response = session.post(
            "/v1/chat/completions",
            session.body(
                messages=[{"role": "user", "content": ASK_WEATHER}],
                tools=[WEATHER, CLOCK],
                tool_choice=value,
                max_tokens=256,
            ),
        )
        calls = _calls(response.body)
        statuses[mode] = response.status
        called[mode] = bool(calls)
        names[mode] = [dig(call, "function.name") for call in calls]
        finish[mode] = response.at("choices.0.finish_reason")

        if calls:
            if not fields:
                fields = sorted(k for k in calls[0] if not k.startswith("__"))
            parsed, conforms, problem = _arguments(calls[0])
            valid[mode] = parsed and conforms
            if problem:
                problems[mode] = problem
        else:
            valid[mode] = False

    return {
        "statuses": statuses,
        "called": called,
        "finish_reasons": finish,
        "call_fields": fields,
        "arguments_valid": valid,
        "tool_names": names,
        "problems": problems,
    }


@probe(
    id="tools.streaming",
    title="A streamed tool call reassembles into valid arguments",
    requirement="I. Tool Calling — streaming function calling; thinking must not contaminate arguments",
    compare=("status", "streamed", "fragmented", "has_index", "reassembles", "schema_valid"),
)
def streaming(session: Session) -> dict[str, Any]:
    """Reassembles the argument fragments the way a client must, and checks the result.

    This is where an endpoint that leaks reasoning text into the argument channel is
    caught: the fragments concatenate into something that is no longer JSON. That is
    why `reassembles` and `schema_valid` are separate observations — the first fails
    on contamination, the second on a model that simply got the schema wrong.
    """
    stream = session.stream(
        session.body(
            messages=[{"role": "user", "content": ASK_WEATHER}],
            tools=[WEATHER],
            tool_choice="required",
            max_tokens=256,
        )
    )

    fragments: dict[int, list[str]] = {}
    names: dict[int, str] = {}
    saw_index = False
    for chunk in stream.chunks:
        for call in dig(chunk, "choices.0.delta.tool_calls") or []:
            if not isinstance(call, dict):
                continue
            index = call.get("index", 0)
            if "index" in call:
                saw_index = True
            name = dig(call, "function.name")
            if name:
                names[index] = name
            piece = dig(call, "function.arguments")
            if isinstance(piece, str):
                fragments.setdefault(index, []).append(piece)

    joined = {index: "".join(pieces) for index, pieces in fragments.items()}
    reassembles, valid, problem = False, False, ""
    if joined:
        first = joined[min(joined)]
        try:
            parsed = json.loads(first)
            reassembles = True
            jsonschema.validate(parsed, WEATHER["function"]["parameters"])
            valid = True
        except json.JSONDecodeError as exc:
            problem = f"reassembled arguments are not JSON: {exc}"
        except jsonschema.ValidationError as exc:
            problem = f"schema violation: {exc.message}"

    return {
        "status": stream.status,
        "streamed": bool(fragments),
        "fragmented": any(len(pieces) > 1 for pieces in fragments.values()),
        "has_index": saw_index,
        "reassembles": reassembles,
        "schema_valid": valid,
        "call_count": len(joined),
        "problem": problem,
    }


@probe(
    id="tools.round_trip",
    title="A tool result sent back produces a final answer that uses it",
    requirement="I. Tool Calling — tool-result return support; multi-turn tool conversations",
    compare=("status", "accepted", "has_content", "used_result", "finish_reason"),
)
def round_trip(session: Session) -> dict[str, Any]:
    """Feeds a fabricated result back and checks the model actually read it.

    The sentinel is a temperature no model would invent, so `used_result` is a real
    observation rather than a coincidence. Whether the *first* turn produced a call
    is not this probe's question — if it did not, the probe records that and stops,
    because a round trip with nothing to return is not a failure of the round trip.
    """
    first = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[{"role": "user", "content": ASK_WEATHER}],
            tools=[WEATHER],
            tool_choice="required",
            max_tokens=256,
        ),
    )
    calls = _calls(first.body)
    if not calls:
        return {
            "status": first.status,
            "accepted": None,
            "has_content": False,
            "used_result": False,
            "finish_reason": None,
            "no_call_on_first_turn": True,
        }

    assistant = dig(first.body, "choices.0.message")
    call_id = calls[0].get("id") or "call_0"
    second = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[
                {"role": "user", "content": ASK_WEATHER},
                {k: v for k, v in assistant.items() if not k.startswith("__")},
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps({"city": "Lisbon", "temperature_c": 41, "sky": "clear"}),
                },
            ],
            tools=[WEATHER],
            max_tokens=256,
        ),
    )
    content = second.at("choices.0.message.content") or ""

    return {
        "status": second.status,
        "accepted": second.ok,
        "has_content": bool(content.strip()),
        "used_result": "41" in content,
        "finish_reason": second.at("choices.0.finish_reason"),
        "no_call_on_first_turn": False,
    }


@probe(
    id="tools.parallel",
    title="A prompt needing two independent tools, and how many calls come back",
    requirement="I. Tool Calling — sequential and parallel calls",
    compare=("status", "call_shape", "distinct_tools"),
)
def parallel(session: Session) -> dict[str, Any]:
    """Counts collapsed to a shape, because a count is not stable between honest runs.

    A model may answer this prompt with two calls in one turn, or with one call and
    a plan to make the second next turn. Both are correct. What is comparable is the
    coarse shape — none, one, or several — and whether the endpoint is structurally
    capable of returning more than one call at a time.
    """
    response = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[{"role": "user", "content": ASK_BOTH}],
            tools=[WEATHER, CLOCK],
            tool_choice="auto",
            max_tokens=512,
        ),
    )
    calls = _calls(response.body)
    names = {dig(call, "function.name") for call in calls}
    shape = "none" if not calls else "one" if len(calls) == 1 else "several"

    return {
        "status": response.status,
        "call_shape": shape,
        "distinct_tools": len(names),
        "call_count": len(calls),
        "tool_names": sorted(name for name in names if name),
    }


@probe(
    id="tools.restraint",
    title="Whether tools are called when no tool applies, and whether undeclared ones are invented",
    requirement="I. Tool Calling — robustness: no tool on casual conversation, no undefined functions",
    compare=("status", "called_on_small_talk", "invented_function", "empty_tools_status"),
)
def restraint(session: Session) -> dict[str, Any]:
    """The two hallucination failures the standard names, plus the empty-tools edge case.

    `invented_function` is the one worth the trouble: an endpoint that returns a call
    to a name that was never declared is broken in a way that passes every other
    check here, because the call is well-formed JSON with a plausible name.
    """
    declared = {WEATHER["function"]["name"], CLOCK["function"]["name"]}

    chat = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[{"role": "user", "content": SMALL_TALK}],
            tools=[WEATHER, CLOCK],
            tool_choice="auto",
            max_tokens=64,
        ),
    )
    chat_calls = _calls(chat.body)

    unrelated = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[{"role": "user", "content": "Book me a flight to Tokyo."}],
            tools=[WEATHER],
            tool_choice="auto",
            max_tokens=256,
        ),
    )
    unrelated_calls = _calls(unrelated.body)
    invented = [
        name
        for call in chat_calls + unrelated_calls
        if (name := dig(call, "function.name")) and name not in declared
    ]

    empty = session.post(
        "/v1/chat/completions",
        session.body(messages=[{"role": "user", "content": SMALL_TALK}], tools=[], max_tokens=32),
    )

    return {
        "status": chat.status,
        "called_on_small_talk": bool(chat_calls),
        "called_on_unmatched_request": bool(unrelated_calls),
        "invented_function": bool(invented),
        "invented_names": sorted(set(invented)),
        "empty_tools_status": empty.status,
    }
