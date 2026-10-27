"""What an error body says, and whether it says who is really serving the request.

The leak check is the one probe here whose needles come from our side instead of
the model's: the profile lists the strings that would give us away, our hostnames,
our project names, the upstream image name, and the probe reports which of them
appear in an error body. That is why it cannot be a parity check. The original
provider will never leak our name, so a diff against it is uninformative; what
matters is the absolute answer.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.session import dig
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import probe


@probe(
    id="errors.shape",
    title="The status and body shape of a rejected request",
    requirement="Error responses, and their structure",
    compare=(
        "bad_parameter_status",
        "unknown_model_status",
        "malformed_status",
        "error_fields",
        "envelopes",
        "consistent_envelope",
    ),
)
def shape(session: Session) -> dict[str, Any]:
    bad_parameter = session.post("/v1/chat/completions", session.body(temperature=9000))
    unknown_model = session.post("/v1/chat/completions", session.body(model="hgrconform-no-such-model"))
    malformed = session.post("/v1/chat/completions", {"model": session.profile.model})

    responses = {
        "bad_parameter": bad_parameter,
        "unknown_model": unknown_model,
        "malformed": malformed,
    }
    envelopes = {
        name: "nested_error"
        if isinstance(response.at("error"), dict)
        else "top_level_error"
        if isinstance(response.body, dict) and isinstance(response.body.get("message"), str)
        else "unknown"
        for name, response in responses.items()
    }
    error = bad_parameter.at("error")
    return {
        "bad_parameter_status": bad_parameter.status,
        "unknown_model_status": unknown_model.status,
        "malformed_status": malformed.status,
        "error_fields": sorted(k for k in error if not k.startswith("__")) if isinstance(error, dict) else [],
        "envelopes": envelopes,
        "consistent_envelope": len(set(envelopes.values())) == 1,
    }


@probe(
    id="errors.leakage",
    title="Whether an error body names the infrastructure actually serving the request",
    requirement="Error messages do not name the infrastructure serving the request",
    compare=(),
)
def leakage(session: Session) -> dict[str, Any]:
    """Not compared against the reference, see this module's docstring.

    The probe set is wider than the errors in its name. A refusal is where a name
    leaks most often and not the only place it can: the catalog is the one response
    that can be read without sending a request, a redirect carries the address of
    whatever built it, and a response header is written by every hop on the path
    whether or not the status is an error. All four are checked here, not
    split across probes, because the finding is the same finding wherever the
    string turns up and a client does not care which probe found it.

    `where` names the request, and `in_headers` says whether it was a header rather
    than a body: the recorded exchange keeps no response headers, so a header leak
    would otherwise be a finding nobody could reproduce from the artifact.
    """
    needles = session.profile.leak_needles
    if not needles:
        raise Skip("profile declares no leak_needles")

    probes_sent = [
        session.post("/v1/chat/completions", session.body(model="hgrconform-no-such-model")),
        session.post("/v1/chat/completions", {"model": session.profile.model}),
        session.get("/v1/hgrconform-no-such-route"),
        # The catalog. Answered without an engine and by whichever hop owns the
        # model list, so it is the one response whose every field is written by us.
        session.get("/v1/models"),
        # A trailing slash. A framework answers this with a redirect whose Location
        # it builds from the Host header it was reached on, which is the address of
        # the hop that dialled it and not one a client may have.
        session.get("/v1/models/"),
        # Not part of the served API, and that is the point: if it answers at all,
        # it answers with whatever an internal probe was written to say.
        session.get("/readyz"),
    ]

    found: dict[str, list[str]] = {}
    in_headers: dict[str, list[str]] = {}
    for response in probes_sent:
        headers = " ".join(f"{k}: {v}" for k, v in response.headers.items())
        for needle in needles:
            lowered = needle.lower()
            if lowered in headers.lower():
                found.setdefault(needle, []).append(response.path)
                in_headers.setdefault(needle, []).append(response.path)
            elif lowered in response.text.lower():
                found.setdefault(needle, []).append(response.path)

    return {
        "needles_checked": list(needles),
        "needles_found": sorted(found),
        "leaked": bool(found),
        "where": {needle: sorted(set(paths)) for needle, paths in found.items()},
        "in_headers": {needle: sorted(set(paths)) for needle, paths in in_headers.items()},
    }


# The classes the OpenAI error envelope uses. An endpoint is free to add to this set;
# what it may not do is put an HTTP reason phrase here, which is what a framework's
# default handler does when nothing has mapped the error into the vendor's own
# vocabulary. That is the difference this list exists to make observable.
ERROR_CLASSES = (
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "resource_not_found_error",
    "rate_limit_error",
    "server_error",
    "api_error",
    "overloaded_error",
)


def _envelope(response: Any) -> dict[str, Any]:
    """The types of an error envelope's own fields, instead of their values.

    `type` and `code` are compared as *kinds*, is `type` one of the vendor error
    classes or something else, is `code` a string or a number, because their values
    legitimately differ between two endpoints and their types do not. A `code` given
    as the integer 400 is the HTTP status wearing the name of a field the spec
    defines as a string, and a client that switches on it breaks on the endpoint that
    spells it properly.
    """
    error = dig(response.body, "error")
    if not isinstance(error, dict):
        return {
            "envelope": "none" if response.ok else "unstructured",
            "type_kind": "absent",
            "code_kind": "absent",
        }
    kind = error.get("type")
    code = error.get("code")
    if code is None:
        code_kind = "null"
    elif isinstance(code, bool):
        code_kind = "boolean"
    elif isinstance(code, (int, float)):
        code_kind = "number"
    elif isinstance(code, str):
        code_kind = "string"
    else:
        code_kind = "other"
    return {
        "envelope": "object",
        "type_kind": (
            "absent"
            if kind is None
            else "error_class"
            if kind in ERROR_CLASSES
            else "http_reason_phrase"
            if isinstance(kind, str) and " " in kind
            else "other"
        ),
        "code_kind": code_kind,
    }


@probe(
    id="errors.envelope_types",
    title="Whether every rejection uses one error vocabulary, whichever component rejected it",
    requirement="Error responses, and their structure",
    compare=("type_kinds", "code_kinds", "one_vocabulary", "statuses"),
)
def envelope_types(session: Session) -> dict[str, Any]:
    """One envelope contract, asked of rejections that are refused at different depths.

    A deployment is not one program. A request can be turned away by whatever stands
    at the edge, or travel to the engine and be turned away there, and those are
    different pieces of software with different ideas about what an error looks like.
    The edge is usually the one that was written against the vendor's envelope,
    because that is the surface somebody specified; the engine answers in its own
    framework's default, and the caller cannot tell which one they reached.

    So the cases here are chosen to be refused at different depths, not to
    cover different kinds of mistake. `pinned` is a value an edge policy typically
    rejects on sight. `negative_max_tokens` and `bad_logit_bias` are well-formed
    enough to be forwarded and are refused by whatever is actually running the model.
    `unknown_model` is refused by routing. If one endpoint answers all four in one
    vocabulary and another switches vocabulary halfway down its own stack,
    `one_vocabulary` is the row that says so, and it needs no reference to be read.

    Statuses are compared but the messages never are: an engine's wording is not a
    contract, while the shape of the envelope carrying it is.
    """
    cases = {
        "pinned": session.body(temperature=9000, max_tokens=8),
        "negative_max_tokens": session.body(max_tokens=-1),
        "bad_logit_bias": session.body(max_tokens=8, logit_bias={"not_a_token_id": "not_a_number"}),
        "unknown_model": session.body(max_tokens=8, model="hgrconform-no-such-model"),
    }

    statuses: dict[str, int] = {}
    type_kinds: dict[str, Any] = {}
    code_kinds: dict[str, Any] = {}
    envelopes: dict[str, Any] = {}
    excerpts: dict[str, str] = {}

    for name, body in cases.items():
        response = session.post("/v1/chat/completions", body)
        shape = _envelope(response)
        statuses[name] = response.status
        type_kinds[name] = shape["type_kind"]
        code_kinds[name] = shape["code_kind"]
        envelopes[name] = shape["envelope"]
        excerpts[name] = response.text[:300]

    rejected = [name for name, status in statuses.items() if status >= 400]
    vocabularies = {type_kinds[name] for name in rejected}
    return {
        "statuses": statuses,
        "type_kinds": type_kinds,
        "code_kinds": code_kinds,
        "envelopes": envelopes,
        "one_vocabulary": len(vocabularies) <= 1,
        "vocabularies": sorted(vocabularies),
        "rejected_cases": sorted(rejected),
        "excerpts": excerpts,
    }


@probe(
    id="errors.required_fields",
    title="Whether a request missing a required field is rejected at all",
    requirement="Error responses, and their structure",
    compare=("statuses", "rejected"),
)
def required_fields(session: Session) -> dict[str, Any]:
    """A rejection that does not happen is the one an error probe cannot see.

    Every other case in this module sends something wrong and compares how the
    refusal is worded. This one sends something incomplete and asks whether a refusal
    happens, because the failure worth catching is the opposite shape: an endpoint
    that accepts `{"model": ...}` with no `messages` and answers 200 has invented a
    conversation the caller never sent, and every field-level check downstream passes
    while doing so. There is no error envelope to compare: the finding is the status.
    """
    cases = {
        "no_messages": {"model": session.profile.model},
        "empty_messages": {"model": session.profile.model, "messages": []},
        "no_model": {"messages": [{"role": "user", "content": "Say OK."}]},
    }
    statuses = {name: session.post("/v1/chat/completions", body).status for name, body in cases.items()}
    return {
        "statuses": statuses,
        "rejected": {name: status >= 400 for name, status in statuses.items()},
    }
