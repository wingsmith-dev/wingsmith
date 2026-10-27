"""Tool calling: the protocol, the round trip, the identity of a call, and the two ways it goes wrong.

Five questions land here, and they are not the same kind of question.
Protocol and round trip are mechanical, either the endpoint accepts the four
`tool_choice` modes and reassembles a streamed call correctly, or it does not.
Parallel calls and hallucination are behavioural, and behaviour varies between two
accurate runs of the same model.

That difference decides what may be compared. `tool_choice: required` producing no
call at all is a protocol defect and is comparable. Everything the model decides for
itself is not: whether it calls a tool under `auto`, how many it calls, whether it
calls one on small talk or invents one, and whether it used a result it was given.
Each of those is a single sample of the model's judgement, two correct runs disagree
on it, and it appears only as evidence.

What is never compared is the argument text. Two endpoints serving one model will
phrase `{"city": "Lisbon"}` and `{"city":"Lisbon"}` differently and mean the same
thing; what matters is that it parsed and that it satisfied the schema we sent.

The identity of a call is the fifth question and the one that needs the most care to
ask. A tool call id is the only thing pairing a result with the call it answers, so
it must be unique for as long as the conversation lives, but every probe that looks
at a single response sees an id that is unique within that response and learns
nothing. A parser that restarts its counter at every assistant turn mints
`get_weather:0` three turns running and passes every mechanical check above while
making the conversation unusable, because the second result a client returns names
the first call. That is only visible across turns, and only to a probe that keeps
every id it was given. `tools.id_uniqueness` and `tools.messages_id_uniqueness` are
the same question asked of the two surfaces on which a client would carry a
conversation, because a gateway can translate one surface from the other and inherit
its ids, or mint its own and diverge.

An id is a generated string and so may never itself be compared. What is compared is
whether the ids collided, and their *shape*: an id built out of the tool name and a
small integer is the fingerprint of a counter that is scoped to one response, and it
is worth reporting even on the run where three turns happened not to collide.
"""

from __future__ import annotations

import re
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

TURN_CITIES = ("Lisbon", "Tokyo", "Paris")
MESSAGES_PATH = "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
INDEX_SUFFIX = re.compile(r"^[^0-9A-Za-z]?\d+$")
DECLARED = (WEATHER["function"]["name"], CLOCK["function"]["name"])


def _plain(value: Any) -> Any:
    """A parsed body with the parser's own bookkeeping keys removed, ready to send back.

    The session records a repeated JSON key under a private marker so the duplicate
    field requirement stays observable. That marker must not survive into a request:
    an assistant message echoed back verbatim carries it, nested inside whatever
    depth the endpoint put it at, and the endpoint would be asked to accept a field
    it never sent.
    """
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if not k.startswith("__")}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    """The same tool declaration in the Messages API's own spelling.

    Translated, not written out a second time, so that the two id probes are
    provably asking about one tool and cannot drift into asking about two.
    """
    function = tool["function"]
    return {
        "name": function["name"],
        "description": function.get("description", ""),
        "input_schema": function["parameters"],
    }


def _id_shape(value: Any, names: tuple[str, ...]) -> str:
    """One tool call id, classified into the kinds that differ in how long they stay unique.

    `opaque` is the only shape that carries no relationship to the call it names, and
    so the only one that can be unique for the life of a conversation without the
    minting side holding any state. The other three are derived from the tool name
    and are therefore only as unique as whatever counter was appended to it, which
    is exactly the thing a single response cannot show.
    """
    if not isinstance(value, str):
        return "absent"
    if not value:
        return "empty"
    for name in names:
        if value == name:
            return "bare_name"
        if value.startswith(name):
            return "name_index" if INDEX_SUFFIX.match(value[len(name) :]) else "name_prefixed"
    return "opaque"


def _identity(ids: list[Any], names: tuple[str, ...]) -> dict[str, Any]:
    """What a run of turns saw of the ids it was given, as facts and nothing else.

    `unique_across_turns` is `None` instead of `False` when no call was made at all,
    because a conversation with nothing to pair is not a pairing defect and must not
    diff as one. The shape collapses to a single word where every id agrees and to
    `mixed` where they do not, which is itself a finding: an endpoint that mints one
    kind of id on the first turn and another kind afterwards is translating rather
    than generating.
    """
    seen = [value for value in ids if isinstance(value, str) and value]
    shapes = {_id_shape(value, names) for value in ids}
    derived = {"bare_name", "name_index", "name_prefixed"}
    if not shapes:
        collapsed = "none"
    elif len(shapes) == 1:
        collapsed = next(iter(shapes))
    else:
        collapsed = "mixed"
    return {
        "unique_across_turns": None if not seen else len(set(seen)) == len(seen),
        "id_shape": collapsed,
        "derived_from_tool_name": bool(shapes & derived),
        "unique_id_count": len(set(seen)),
        "total_id_count": len(ids),
        "repeated_ids": sorted({value for value in seen if seen.count(value) > 1}),
        "ids": ids,
    }


def _calls(response: Any) -> list[dict[str, Any]]:
    raw = dig(response, "choices.0.message.tool_calls") or []
    return [call for call in raw if isinstance(call, dict)]


def _arguments(call: dict[str, Any]) -> tuple[bool, bool, str]:
    """Whether one call's arguments parse, and whether they satisfy the schema we sent.

    Returned as two booleans instead of the argument string, because the string is
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


def _contractual(per_mode: dict[str, Any]) -> dict[str, Any]:
    """A per-mode map without `auto`, where the request leaves the outcome to the model."""
    return {mode: value for mode, value in per_mode.items() if mode != "auto"}


@probe(
    id="tools.protocol",
    title="The four tool_choice modes, and the structure of a returned call",
    requirement="Tool call structure under the four tool_choice modes",
    compare=("statuses", "called", "finish_reasons", "call_fields", "arguments_valid"),
)
def protocol(session: Session) -> dict[str, Any]:
    """Each mode is a separate contract, and an endpoint can honour some and not others.

    `called` is a boolean per mode instead of a count, because the contract is about
    whether a call happened at all: `none` must not call, `required` and a named
    choice must. `auto` leaves the call to the model, so what it did is recorded under
    `auto_*` as evidence and kept out of the compared maps.
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
        "called": _contractual(called),
        "finish_reasons": _contractual(finish),
        "call_fields": fields,
        "arguments_valid": _contractual(valid),
        "auto_called": called["auto"],
        "auto_finish_reason": finish["auto"],
        "auto_arguments_valid": valid["auto"],
        "tool_names": names,
        "problems": problems,
    }


@probe(
    id="tools.streaming",
    title="A streamed tool call reassembles into valid arguments",
    requirement="Streamed tool calls reassemble into valid arguments, uncontaminated by thinking",
    compare=("status", "streamed", "fragmented", "has_index", "reassembles", "schema_valid"),
)
def streaming(session: Session) -> dict[str, Any]:
    """Reassembles the argument fragments the way a client must, and checks the result.

    This is where an endpoint that leaks reasoning text into the argument channel is
    caught: the fragments concatenate into something that is no longer JSON. That is
    why `reassembles` and `schema_valid` are separate observations: the first fails
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
    requirement="A tool result can be returned, and the conversation continues from it",
    compare=(
        "status",
        "accepted",
        "has_content",
        "finish_reason",
        "minted_id_shape",
        "pairable_id",
    ),
)
def round_trip(session: Session) -> dict[str, Any]:
    """Feeds a fabricated result back and checks the model actually read it.

    The sentinel is a temperature no model would invent, so `used_result` is a real
    observation instead of a coincidence. It is still the model's answer, one sample
    of it, so it is evidence and not compared; acceptance of the result is. Whether
    the *first* turn produced a call is not this probe's question, if it did not, the probe records that and stops,
    because a round trip with nothing to return is not a failure of the round trip.

    One turn can say something about identity even though it cannot say whether ids
    stay unique. `pairable_id` is whether the id this probe echoed was the endpoint's
    own: a `tool_call_id` invented here because the endpoint minted none would pair
    with nothing, and an endpoint can answer such a request happily and still be
    unusable to a client that has more than one call outstanding. `minted_id_shape`
    is the first half of the finding `tools.id_uniqueness` completes.
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
            "minted_id_shape": "none",
            "pairable_id": None,
            "no_call_on_first_turn": True,
        }

    assistant = dig(first.body, "choices.0.message")
    minted = calls[0].get("id")
    call_id = minted or "call_0"
    second = session.post(
        "/v1/chat/completions",
        session.body(
            messages=[
                {"role": "user", "content": ASK_WEATHER},
                _plain(assistant),
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
        "minted_id_shape": _id_shape(minted, DECLARED),
        "pairable_id": isinstance(minted, str) and bool(minted),
        "minted_id": minted,
        "no_call_on_first_turn": False,
    }


@probe(
    id="tools.id_uniqueness",
    title="Whether tool call ids stay unique across turns of one conversation",
    requirement="Tool call ids stay unique across the turns of one conversation",
    compare=("statuses", "called_every_turn", "unique_across_turns", "id_shape", "derived_from_tool_name"),
)
def id_uniqueness(session: Session) -> dict[str, Any]:
    """Three turns of the same tool, each result fed back, keeping every id it was given.

    Three is the smallest number that separates the two things a shorter run
    conflates. One turn shows an id and says nothing. Two turns show whether the
    second is a repeat, which a counter that resets per response and a counter that
    is merely low both satisfy. Three shows the pattern.

    The same tool every turn. A conversation that alternates tools would
    hide a per-tool counter behind two names that never collide with each other, and
    a per-tool counter is the defect this exists to see.

    `tool_choice: required` on every turn, so the run does not depend on the model
    choosing to call again after it already has an answer. That makes the turn count
    a contract instead of a behaviour, and `called_every_turn` comparable.
    """
    messages: list[dict[str, Any]] = []
    ids: list[Any] = []
    statuses: list[int] = []
    calls_per_turn: list[int] = []

    for index, city in enumerate(TURN_CITIES):
        prompt = f"What is the weather in {city}? Use the tool." if not index else f"And in {city}? Use the tool."
        messages.append({"role": "user", "content": prompt})
        response = session.post(
            "/v1/chat/completions",
            session.body(
                messages=messages,
                tools=[WEATHER],
                tool_choice="required",
                max_tokens=256,
            ),
        )
        statuses.append(response.status)
        calls = _calls(response.body)
        calls_per_turn.append(len(calls))
        if not calls:
            break

        ids.extend(call.get("id") for call in calls)
        messages.append(_plain(dig(response.body, "choices.0.message")))
        for call in calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{len(ids)}",
                    "content": json.dumps({"city": city, "temperature_c": 41, "sky": "clear"}),
                }
            )

    return {
        "statuses": statuses,
        "turns_requested": len(TURN_CITIES),
        "calls_per_turn": calls_per_turn,
        "called_every_turn": len(calls_per_turn) == len(TURN_CITIES) and all(calls_per_turn),
        **_identity(ids, DECLARED),
    }


@probe(
    id="tools.messages_id_uniqueness",
    title="The same question on the Anthropic Messages surface, where a client pairs by tool_use_id",
    requirement="Tool use ids stay unique across turns on the Anthropic Messages surface",
    compare=(
        "statuses",
        "called_every_turn",
        "unique_across_turns",
        "id_shape",
        "derived_from_tool_name",
        "stop_reasons",
    ),
    requires=("messages",),
)
def messages_id_uniqueness(session: Session) -> dict[str, Any]:
    """Asked separately because the surface can answer it differently, and matters more here.

    `/v1/chat/completions` carries a tool result in its own message role, so a client
    that loses track of an id can still be understood by position. The Messages API
    cannot: a `tool_result` block names a `tool_use_id` and nothing else, and a
    conversation carrying two blocks with one id is malformed, not ambiguous.
    So a gateway that translates this surface out of the other one and inherits its
    ids inherits the defect into the place where it is fatal.

    Blocks are echoed back exactly as they arrived, thinking blocks included, because
    a client does the same and an endpoint that rejects its own output is the finding.
    """
    tool = _anthropic(WEATHER)
    headers = session.headers | {"anthropic-version": ANTHROPIC_VERSION}
    messages: list[dict[str, Any]] = []
    ids: list[Any] = []
    statuses: list[int] = []
    stop_reasons: list[Any] = []
    uses_per_turn: list[int] = []

    for index, city in enumerate(TURN_CITIES):
        prompt = f"What is the weather in {city}? Use the tool." if not index else f"And in {city}? Use the tool."
        messages.append({"role": "user", "content": prompt})
        response = session.post(
            MESSAGES_PATH,
            {
                "model": session.profile.model,
                "max_tokens": 512,
                "messages": messages,
                "tools": [tool],
                "tool_choice": {"type": "any"},
            },
            headers=headers,
        )
        statuses.append(response.status)
        stop_reasons.append(response.at("stop_reason"))
        blocks = [block for block in (response.at("content") or []) if isinstance(block, dict)]
        uses = [block for block in blocks if block.get("type") == "tool_use"]
        uses_per_turn.append(len(uses))
        if not uses:
            break

        ids.extend(use.get("id") for use in uses)
        messages.append({"role": "assistant", "content": _plain(blocks)})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": use.get("id") or f"toolu_{len(ids)}",
                        "content": json.dumps({"city": city, "temperature_c": 41, "sky": "clear"}),
                    }
                    for use in uses
                ],
            }
        )

    return {
        "statuses": statuses,
        "stop_reasons": stop_reasons,
        "turns_requested": len(TURN_CITIES),
        "uses_per_turn": uses_per_turn,
        "called_every_turn": len(uses_per_turn) == len(TURN_CITIES) and all(uses_per_turn),
        **_identity(ids, DECLARED),
    }


@probe(
    id="tools.parallel",
    title="A prompt needing two independent tools, and how many calls come back",
    requirement="Sequential and parallel tool calls",
    compare=("status",),
)
def parallel(session: Session) -> dict[str, Any]:
    """Whether the request is accepted, with what the model did with it as evidence.

    A model may answer this prompt with two calls in one turn, or with one call and
    a plan to make the second next turn. Both are correct, and which one a single
    request gets is the model's choice, so the shape (none, one, or several) is
    recorded but not compared. Only the status is.
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
    requirement="No tool call on casual conversation, and no call to an undeclared function",
    compare=("status", "empty_tools_status"),
)
def restraint(session: Session) -> dict[str, Any]:
    """The two hallucination failures, plus the empty-tools edge case.

    `invented_function` is the one worth reading: a call to a name that was never
    declared passes every other check here, because it is well-formed JSON with a
    plausible name. Both hallucination readings are one sample of the model's
    judgement, so they are evidence for a person and not compared; what is compared is
    how the endpoint answers the requests, including one that declares no tools.
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
        name for call in chat_calls + unrelated_calls if (name := dig(call, "function.name")) and name not in declared
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


ALLOWED_TOOLS_MODES = ("auto", "required")


def _allowed_tools(mode: str, names: tuple[str, ...]) -> dict[str, Any]:
    """`tool_choice` in the form that narrows the callable set without naming one call.

    Built from the same declarations the rest of this module sends, so the probe
    cannot drift into constraining a tool the request never offered.
    """
    return {
        "type": "allowed_tools",
        "allowed_tools": {
            "mode": mode,
            "tools": [{"type": "function", "function": {"name": name}} for name in names],
        },
    }


@probe(
    id="tools.allowed_tools",
    title="tool_choice as a restricted set instead of a single name",
    requirement="tool_choice restricted to a subset of the declared tools",
    compare=("statuses", "called", "restricted", "error_shape", "error_type"),
)
def allowed_tools(session: Session) -> dict[str, Any]:
    """The fifth `tool_choice` mode, which `tools.protocol` does not reach.

    `tools.protocol` asks about the four modes that have existed since the beginning;
    this asks about the object form that constrains *which* tools may be called while
    leaving the choice among them to the model. It is a separate probe instead of a
    fifth row there because it fails differently: the four modes are a string or a
    named-function object that every OpenAI-compatible decoder has a variant for,
    while this one is a newer variant an older decoder rejects at the parse, before
    any policy in front of it has an opinion. Folding it in would have put a decoder
    failure and a policy refusal in one comparable key.

    Three questions, and only the first two are strongly comparable. Whether the shape
    is accepted at all is mechanical. Whether a call happened under `required` is
    contractual. Whether the call that happened respects the restriction is the point
    of the feature: recorded as `restricted`, which is true when every call names a
    tool from the allowed set, and vacuously true when no call was made, so it reads
    only alongside `called`.

    The third mode names a tool absent from `tools`. That is a request a caller can
    write by mistake, and endpoints disagree on whether it is an error or a narrowing
    to the empty set; the answer is recorded, not judged.
    """
    cases: dict[str, Any] = {
        "auto": _allowed_tools("auto", (WEATHER["function"]["name"],)),
        "required": _allowed_tools("required", (WEATHER["function"]["name"],)),
        "undeclared": _allowed_tools("required", ("get_stock_price",)),
    }

    statuses: dict[str, int] = {}
    called: dict[str, bool] = {}
    restricted: dict[str, bool] = {}
    names: dict[str, list[Any]] = {}
    shapes: dict[str, Any] = {}
    types: dict[str, Any] = {}
    excerpts: dict[str, str] = {}

    for case, choice in cases.items():
        permitted = {entry["function"]["name"] for entry in choice["allowed_tools"]["tools"] if isinstance(entry, dict)}
        response = session.post(
            "/v1/chat/completions",
            session.body(
                messages=[{"role": "user", "content": ASK_WEATHER}],
                tools=[WEATHER, CLOCK],
                tool_choice=choice,
                max_tokens=256,
            ),
        )
        calls = _calls(response.body)
        called_names = [dig(call, "function.name") for call in calls]
        statuses[case] = response.status
        called[case] = bool(calls)
        names[case] = called_names
        restricted[case] = all(name in permitted for name in called_names)
        error = dig(response.body, "error")
        shapes[case] = "object" if isinstance(error, dict) else ("none" if response.ok else "unstructured")
        types[case] = error.get("type") if isinstance(error, dict) else None
        if not response.ok:
            excerpts[case] = response.text[:300]

    return {
        "statuses": statuses,
        "called": _contractual(called),
        "auto_called": called["auto"],
        "restricted": restricted,
        "error_shape": shapes,
        "error_type": types,
        "tool_names": names,
        "declared": list(DECLARED),
        "excerpts": excerpts,
    }
