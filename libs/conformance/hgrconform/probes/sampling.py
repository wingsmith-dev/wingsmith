"""Which sampling parameters the endpoint accepts, and at which values it stops accepting them.

The verifier this replaces asserts that particular values are rejected (temperature
above one, `n` above one) because the model it was written for pins them. That is a
vendor policy and not a protocol rule, so asserting it reports a failure against every
other model. The question that survives the change of model is a parity question:
for each parameter, at a legal value, at a value outside the documented range, and at
the wrong type, what status came back. Two endpoints with the same acceptance policy
produce the same map; a difference in the map is the finding.

A rejected unknown or out-of-range parameter and a silently ignored one are both
legitimate, so nothing here judges a status. The one thing a status cannot say is
whether an accepted out-of-range value was honoured or clamped, and that is recorded
separately as evidence, by sending the same prompt twice at each value and reporting
whether the pair came back identical.
"""

from __future__ import annotations

from typing import Any

from hgrconform.session import Session
from hgrconform.session import dig
from hgrconform.probes.registry import probe


CASES: dict[str, dict[str, dict[str, Any]]] = {
    "temperature": {
        "legal": {"temperature": 0.7},
        "out_of_range": {"temperature": 5.0},
        "wrong_type": {"temperature": "hot"},
    },
    "top_p": {
        "legal": {"top_p": 0.9},
        "out_of_range": {"top_p": 3.0},
        "wrong_type": {"top_p": "wide"},
    },
    "n": {
        "legal": {"n": 1},
        "out_of_range": {"n": 0},
        "wrong_type": {"n": "two"},
    },
    "presence_penalty": {
        "legal": {"presence_penalty": 0.5},
        "out_of_range": {"presence_penalty": 5.0},
        "wrong_type": {"presence_penalty": "high"},
    },
    "frequency_penalty": {
        "legal": {"frequency_penalty": 0.5},
        "out_of_range": {"frequency_penalty": -5.0},
        "wrong_type": {"frequency_penalty": "high"},
    },
    "stop": {
        "legal": {"stop": ["\n\n"]},
        "out_of_range": {"stop": [f"s{index}" for index in range(16)]},
        "wrong_type": {"stop": 7},
    },
    "seed": {
        "legal": {"seed": 42},
        "out_of_range": {"seed": 2**63},
        "wrong_type": {"seed": "42"},
    },
    "logprobs": {
        "legal": {"logprobs": True, "top_logprobs": 1},
        "out_of_range": {"logprobs": True, "top_logprobs": 50},
        "wrong_type": {"logprobs": "yes"},
    },
}

CONTENT = "choices.0.message.content"


@probe(
    id="sampling.acceptance",
    title="The status returned for each sampling parameter at a legal, an out-of-range, and a mistyped value",
    requirement="Which request parameters are accepted, and within which bounds",
    compare=("acceptance",),
)
def acceptance(session: Session) -> dict[str, Any]:
    """The acceptance policy as a status map, plus whether an accepted out-of-range value did anything.

    `acceptance` is one flat `parameter:case -> status` dictionary so the diff names
    the exact cell that differs. Response bodies stay out of it: the generated text
    differs between two healthy endpoints for reasons that have nothing to do with
    the policy being compared.

    The clamping evidence sits outside `compare` because it is a second-order
    observation and a noisy one: a model can repeat itself at a high temperature by
    chance on a short completion. It is here because a status of 200 alone cannot
    separate an endpoint that honours `temperature: 5.0` from one that quietly pins
    it, and a caller sees those as different endpoints.
    """
    statuses: dict[str, int] = {}
    detail: dict[str, str] = {}
    for parameter, cases in CASES.items():
        for case, overrides in cases.items():
            response = session.post("/v1/chat/completions", session.body(max_tokens=16, **overrides))
            statuses[f"{parameter}:{case}"] = response.status
            message = dig(response.body, "error.message")
            if isinstance(message, str):
                detail[f"{parameter}:{case}"] = message[:200]

    def twice(**overrides: Any) -> tuple[bool, int]:
        first = session.post("/v1/chat/completions", session.body(max_tokens=24, **overrides))
        second = session.post("/v1/chat/completions", session.body(max_tokens=24, **overrides))
        if not (first.ok and second.ok):
            return False, first.status
        return first.at(CONTENT) == second.at(CONTENT), first.status

    greedy_repeats, greedy_status = twice(temperature=0)
    hot_repeats, hot_status = twice(**CASES["temperature"]["out_of_range"])

    honoured: bool | None = None
    if statuses.get("temperature:out_of_range") == 200 and greedy_status == 200 and hot_status == 200:
        honoured = greedy_repeats and not hot_repeats

    return {
        "acceptance": statuses,
        "error_messages": detail,
        "greedy_repeats": greedy_repeats,
        "out_of_range_repeats": hot_repeats,
        "out_of_range_honoured": honoured,
    }


@probe(
    id="sampling.unknown_parameter",
    title="The status returned for a parameter the endpoint has no name for",
    requirement="How unrecognised request parameters are handled",
    compare=("unknown_status", "unknown_nested_status"),
)
def unknown_parameter(session: Session) -> dict[str, Any]:
    """Rejecting an unknown field and ignoring it are both defensible; only the choice is compared."""
    top_level = session.post("/v1/chat/completions", session.body(max_tokens=8, hgrconform_unknown="x"))
    nested = session.post(
        "/v1/chat/completions",
        session.body(max_tokens=8, stream_options={"hgrconform_unknown": True}),
    )
    return {
        "unknown_status": top_level.status,
        "unknown_nested_status": nested.status,
        "unknown_error_message": (dig(top_level.body, "error.message") or "")[:200],
    }
