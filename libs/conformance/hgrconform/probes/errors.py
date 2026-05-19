"""What an error body says, and whether it says who is really serving the request.

The leak check is the one probe here whose needles come from our side rather than
the model's: the profile lists the strings that would give us away — our hostnames,
our project names, the upstream image name — and the probe reports which of them
appear in an error body. That is why it cannot be a parity check. The original
provider will never leak our name, so a diff against it is uninformative; what
matters is the absolute answer.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import probe


@probe(
    id="errors.shape",
    title="The status and body shape of a rejected request",
    requirement="I. Error Message Information — error responses, and their structure",
    compare=("bad_parameter_status", "unknown_model_status", "malformed_status", "error_fields"),
)
def shape(session: Session) -> dict[str, Any]:
    bad_parameter = session.post("/v1/chat/completions", session.body(temperature=9000))
    unknown_model = session.post("/v1/chat/completions", session.body(model="hgrconform-no-such-model"))
    malformed = session.post("/v1/chat/completions", {"model": session.profile.model})

    error = bad_parameter.at("error")
    return {
        "bad_parameter_status": bad_parameter.status,
        "unknown_model_status": unknown_model.status,
        "malformed_status": malformed.status,
        "error_fields": sorted(k for k in error if not k.startswith("__")) if isinstance(error, dict) else [],
    }


@probe(
    id="errors.leakage",
    title="Whether an error body names the infrastructure actually serving the request",
    requirement="I. Error Message Information — does the error message contain supplier-related information",
    compare=(),
)
def leakage(session: Session) -> dict[str, Any]:
    """Not compared against the reference, deliberately — see this module's docstring."""
    needles = session.profile.leak_needles
    if not needles:
        raise Skip("profile declares no leak_needles")

    probes_sent = [
        session.post("/v1/chat/completions", session.body(model="hgrconform-no-such-model")),
        session.post("/v1/chat/completions", {"model": session.profile.model}),
        session.get("/v1/hgrconform-no-such-route"),
    ]

    found: dict[str, list[str]] = {}
    for response in probes_sent:
        haystack = f"{response.text}\n{' '.join(f'{k}: {v}' for k, v in response.headers.items())}".lower()
        for needle in needles:
            if needle.lower() in haystack:
                found.setdefault(needle, []).append(response.path)

    return {
        "needles_checked": list(needles),
        "needles_found": sorted(found),
        "leaked": bool(found),
        "where": {needle: sorted(set(paths)) for needle, paths in found.items()},
    }
