from __future__ import annotations

import json

from typing import Any
from pathlib import Path

from hgrconform.profile import Profile
from hgrconform.session import Response
from hgrconform.probes.tools import id_uniqueness
from hgrconform.probes.tools import messages_id_uniqueness
from hgrconform.probes.errors import shape
from hgrconform.probes.length import parameters


def response(status: int, body: dict[str, Any]) -> Response:
    return Response(
        status=status,
        headers={"content-type": "application/json"},
        text=json.dumps(body),
        elapsed=0,
        request={},
        path="/v1/chat/completions",
    )


class StubSession:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = iter(responses)
        self.profile = Profile(name="test", base_url="http://test", model="test")
        self.evidence_dir = Path(".")

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def body(self, **overrides: Any) -> dict[str, Any]:
        return {"model": "test"} | overrides

    def post(self, _path: str, _body: dict[str, Any], headers: dict[str, str] | None = None) -> Response:
        return next(self.responses)


def test_error_shape_records_envelope_drift() -> None:
    nested = {"error": {"message": "bad", "type": "invalid_request_error", "code": "bad"}}
    top_level = {"message": "failed", "type": "Internal Server Error", "code": 500}
    observed = shape(StubSession([response(400, top_level), response(404, nested), response(400, nested)]))

    assert observed["envelopes"] == {
        "bad_parameter": "top_level_error",
        "unknown_model": "nested_error",
        "malformed": "nested_error",
    }
    assert observed["consistent_envelope"] is False


def test_length_parameters_record_a_reasoning_only_response() -> None:
    reasoning_only = {
        "choices": [{"message": {"content": None, "reasoning_content": "working"}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 16},
    }
    visible = {
        "choices": [{"message": {"content": "OK", "reasoning_content": "working"}, "finish_reason": "stop"}],
        "usage": {"completion_tokens": 12},
    }
    rejected = {"error": {"message": "unsupported", "type": "invalid_request_error"}}
    observed = parameters(StubSession([response(200, reasoning_only), response(200, visible), response(400, rejected)]))

    assert observed["visible_content"] == {
        "max_tokens": False,
        "max_completion_tokens": True,
        "max_output_tokens": False,
    }
    assert observed["reasoning_only"] == {
        "max_tokens": True,
        "max_completion_tokens": False,
        "max_output_tokens": False,
    }


def tool_call(call_id: str, name: str = "get_weather") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": '{"city": "Lisbon"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def tool_use(use_id: str, name: str = "get_weather") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": use_id, "name": name, "input": {"city": "Lisbon"}}],
        "stop_reason": "tool_use",
    }


def test_id_uniqueness_records_a_counter_that_resets_every_turn() -> None:
    """A per-response counter: unique inside one response, identical across three."""
    turns = [response(200, tool_call("get_weather:0")) for _ in range(3)]
    observed = id_uniqueness(StubSession(turns))

    assert observed["called_every_turn"] is True
    assert observed["unique_across_turns"] is False
    assert observed["id_shape"] == "name_index"
    assert observed["derived_from_tool_name"] is True
    assert observed["unique_id_count"] == 1
    assert observed["total_id_count"] == 3
    assert observed["repeated_ids"] == ["get_weather:0"]


def test_id_uniqueness_records_opaque_ids_as_unique() -> None:
    ids = ["chatcmpl-tool-a6ce2d70271fa55f", "chatcmpl-tool-1f0b9c4d5e6a7b8c", "chatcmpl-tool-9d8c7b6a5f4e3d2c"]
    observed = id_uniqueness(StubSession([response(200, tool_call(value)) for value in ids]))

    assert observed["unique_across_turns"] is True
    assert observed["id_shape"] == "opaque"
    assert observed["derived_from_tool_name"] is False
    assert observed["repeated_ids"] == []


def test_messages_id_uniqueness_reads_tool_use_blocks() -> None:
    """The same defect on the surface where a colliding tool_use_id makes the request malformed."""
    turns = [response(200, tool_use("get_weather:0")) for _ in range(3)]
    observed = messages_id_uniqueness(StubSession(turns))

    assert observed["stop_reasons"] == ["tool_use", "tool_use", "tool_use"]
    assert observed["called_every_turn"] is True
    assert observed["unique_across_turns"] is False
    assert observed["id_shape"] == "name_index"
    assert observed["repeated_ids"] == ["get_weather:0"]


def test_every_eos_bucket_is_reachable_at_the_minimum_sample_size() -> None:
    from hgrconform.probes import eos

    n = eos.MIN_SAMPLES
    reached = {eos._bucket(count, n) for count in range(n + 1)}
    assert reached == {"rare", "occasional", "frequent"}
    assert eos._bucket(0, 0) == "no-samples"


def test_one_empty_sample_never_moves_the_eos_bucket() -> None:
    from hgrconform.probes import eos

    for n in (eos.MIN_SAMPLES, eos.SAMPLES, 200):
        assert eos._bucket(0, n) == eos._bucket(1, n) == "rare"


def test_a_small_eos_override_is_raised_to_the_minimum(monkeypatch) -> None:
    from hgrconform.probes import eos

    monkeypatch.setenv(eos.SAMPLES_ENV, "5")
    assert eos.sample_size() == eos.MIN_SAMPLES
    monkeypatch.setenv(eos.SAMPLES_ENV, "200")
    assert eos.sample_size() == 200
    monkeypatch.delenv(eos.SAMPLES_ENV)
    assert eos.sample_size() == eos.SAMPLES
