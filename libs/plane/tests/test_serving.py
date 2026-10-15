"""The serving configuration a workload carries, and everything that reads it.

These tests were written against `Recipe` and moved here with the fields, because the
object went and the rules did not: a dtype in the quantization box is still a worker
that never binds a port, a device count that does not divide the parallel layout is
still unplaceable, and a serving block that contradicts its own capability claims is
still a lie the plane can catch before a cluster does.

What is new here is `serving_digest`. The identity of a serving configuration is its
content, so the tests that used to ask whether two recipes were the same object now ask
whether two workloads hash the same — including across a write and the reload after it,
which is a question a separate object never had to answer.

The workload's own surface — its request shapes, its routes, its repository — is
`test_workloads.py`. This file is the configuration, not the object that holds it.
"""

import json
import hashlib
import datetime as dt

from types import SimpleNamespace
from decimal import Decimal

import pytest
import msgspec

from app.config import settings
from app.core.ids import ARTIFACT
from app.core.ids import WORKLOAD
from app.core.ids import new_uid
from app.core.vram import extract_shape
from app.core.vram import shape_metadata
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import WorkloadRole
from app.core.serving import DIGEST_FIELDS
from app.core.serving import _canonical
from app.core.serving import served_model
from app.core.serving import serving_digest
from app.core.serving import declared_router
from app.core.serving import recipe_engine_args
from app.hub.messages import RouterSpec
from app.hub.messages import ServingSpec
from app.core.exceptions import Invalid
from app.ingestion.desired import _serving
from app.modules.workloads.schemas import RoleSchema
from app.modules.workloads.schemas import CacheSchema
from app.modules.workloads.schemas import ShapeSchema
from app.modules.workloads.schemas import RouterSchema
from app.modules.workloads.schemas import OverlaySchema
from app.modules.workloads.schemas import ServingSchema
from app.modules.workloads.schemas import TopologySchema
from app.modules.workloads.schemas import ReasoningSchema
from app.modules.workloads.schemas import ResourcesSchema
from app.modules.workloads.schemas import ParallelismSchema
from app.modules.workloads.schemas import CapabilitiesSchema
from app.modules.workloads.schemas import WorkloadServingSchema
from app.modules.workloads.schemas import WorkloadServingRequest
from app.modules.workloads.serving import router_block
from app.modules.workloads.serving import serving_args
from app.modules.workloads.serving import serving_view
from app.modules.workloads.serving import apply_serving
from app.modules.workloads.serving import group_devices
from app.modules.workloads.serving import declared_roles
from app.modules.workloads.serving import serving_request
from app.modules.workloads.serving import resolved_engine_args
from app.modules.workloads.serving import resolve_engine_runtime_image
from app.modules.workloads.validation import sizing_for
from app.modules.workloads.validation import check_floor
from app.modules.workloads.validation import check_shape
from app.modules.workloads.validation import check_router
from app.modules.workloads.validation import check_serving
from app.modules.workloads.validation import check_capacity
from app.modules.workloads.validation import check_topology
from app.modules.workloads.validation import check_footprint
from app.modules.workloads.validation import check_quantization
from app.modules.workloads.validation import check_serving_request


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def shape(**kwargs) -> dict:
    base = {
        "device_class": "h100",
        "device_count": 8,
        "tensor_parallel": 8,
        "pipeline_parallel": 1,
        "expert_parallel": 1,
        "context_length": 32768,
        "kv_cache_fraction": None,
    }
    return base | kwargs


def serving(**kwargs) -> ServingSchema:
    reasoning = kwargs.pop("reasoning", None)
    block = ServingSchema(**kwargs)
    if reasoning is not None:
        block.reasoning = ReasoningSchema(**reasoning)
    return block


def block(**kwargs) -> dict:
    """A serving block in the shape the column stores it."""
    return msgspec.to_builtins(serving(**kwargs))


def capabilities(**kwargs) -> CapabilitiesSchema:
    return CapabilitiesSchema(**kwargs)


def a_workload(**kwargs) -> Workload:
    """A Workload row as SQLAlchemy hands one back, with no server defaults applied.

    Every column here is one the recipe used to own. They are set explicitly rather
    than left to the model's defaults because a row read back from Postgres has all
    of them, and a factory that left holes would let a `None` pass a test that the
    database would never produce.
    """
    base = {
        "uid": new_uid(WORKLOAD),
        "name": "llama-vllm",
        "engine": "vllm",
        "engine_runtime_version": "0.6.0",
        "engine_runtime_image": None,
        "frontend_image": None,
        "engine_args": {},
        "imported_args": {},
        "quantization": None,
        "dtype": None,
        "tensor_parallel": 1,
        "pipeline_parallel": 1,
        "expert_parallel": 1,
        "context_length": 8192,
        "max_batch_tokens": None,
        "kv_cache_dtype": None,
        "kv_cache_fraction": None,
        "prefix_cache_enabled": False,
        "prefix_cache_config": {},
        "device_class": "h100",
        "device_count": 1,
        "device_memory_mib_min": 0,
        "requires_single_topology_domain": False,
        "topology_domain_kind": None,
        "serving_topology": "aggregated",
        "cpu_cores": None,
        "memory_mib": None,
        "disk_gib": None,
        "labels": {},
        "overlay": {},
        "serving": {},
        "capabilities": {},
        "serving_digest": "",
        "created_at": EPOCH,
        "updated_at": None,
    }
    return Workload(**(base | kwargs))


def a_request(**kwargs) -> WorkloadServingRequest:
    """The same configuration as `a_workload`, in the shape a caller sends it."""
    fields = {
        "artifact": new_uid(ARTIFACT),
        "engine": "vllm",
        "engine_runtime_version": "0.6.0",
        "context_length": 8192,
        "shape": ShapeSchema(device_class="h100"),
    }
    return WorkloadServingRequest(**(fields | kwargs))


def an_artifact(**kwargs) -> Artifact:
    """An Artifact row with a slug no recipe corpus covers, unless a test asks for one."""
    return Artifact(**({"uid": new_uid(ARTIFACT), "slug": "meta-llama/llama-3.1-8b-instruct"} | kwargs))


def rendered(workload: Workload, artifact: Artifact | None = None):
    """The serving block as a read returns it."""
    return serving_view(workload, artifact or an_artifact())


# --------------------------------------------------------------------------- quantization


def test_a_dtype_in_the_quantization_field_is_refused():
    """`--quantization fp16` is not a degraded serve, it is a worker that never starts.

    vLLM rejects it building the engine config, before the port is bound, so the
    cluster reports only a crash loop and a failing probe. Catching it here is the
    difference between an error on the form and an hour spent in pod logs.
    """
    for value in ("fp16", "bfloat16", "BF16", " float16 ", "auto"):
        with pytest.raises(Invalid):
            check_quantization(value)


def test_a_real_quantization_method_and_an_empty_one_are_both_allowed():
    """Empty is the right answer for unquantized weights, not a missing value."""
    for value in ("mxfp4", "fp8", "awq", "gptq", None, ""):
        check_quantization(value)


# --------------------------------------------------------------------------- shape


def test_a_shape_without_a_device_class_is_not_a_shape():
    """Tensor-parallel-8 means nothing spread across an A100 and an H100."""
    with pytest.raises(Invalid):
        check_shape(**shape(device_class=""))
    with pytest.raises(Invalid):
        check_shape(**shape(device_class="   "))


def test_a_placeable_shape_is_accepted():
    assert check_shape(**shape()) is None
    assert check_shape(**shape(device_count=16, tensor_parallel=8, pipeline_parallel=2)) is None


def test_a_device_count_that_does_not_divide_the_parallel_layout_is_refused():
    with pytest.raises(Invalid):
        check_shape(**shape(device_count=6, tensor_parallel=4))


def test_expert_parallelism_cannot_exceed_the_devices_there_are():
    with pytest.raises(Invalid):
        check_shape(**shape(expert_parallel=16))


@pytest.mark.parametrize("field", ["device_count", "tensor_parallel", "pipeline_parallel", "context_length"])
def test_the_counts_that_must_be_at_least_one(field):
    with pytest.raises(Invalid):
        check_shape(**shape(**{field: 0}))


@pytest.mark.parametrize("fraction", [0, -0.1, 1.5])
def test_kv_cache_fraction_outside_zero_to_one_is_refused(fraction):
    with pytest.raises(Invalid):
        check_shape(**shape(kv_cache_fraction=fraction))


def test_kv_cache_fraction_is_optional_and_a_whole_device_is_allowed():
    assert check_shape(**shape(kv_cache_fraction=None)) is None
    assert check_shape(**shape(kv_cache_fraction=1)) is None


def test_negative_footprint_hints_are_refused():
    assert check_footprint(cpu_cores=None, memory_mib=8, disk_gib=0) is None
    with pytest.raises(Invalid):
        check_footprint(device_memory_mib_min=-1)
    with pytest.raises(Invalid):
        check_footprint(max_batch_tokens=-8)


def test_a_whole_serving_request_is_checked_at_once():
    """One entry point, so a patch cannot arrive at a state a create would have refused.

    Every rule above is reachable through it, and each of them is reached from a
    different corner of the nested request — the shape from `shape`, the parallel
    layout from `parallelism`, the fraction from `cache`.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT), name="llama", digest="sha256:beef")
    assert check_serving_request(a_request(), artifact) is None

    with pytest.raises(Invalid):
        check_serving_request(a_request(quantization="fp16"), artifact)
    with pytest.raises(Invalid):
        check_serving_request(a_request(shape=ShapeSchema(device_class="")), artifact)
    with pytest.raises(Invalid):
        check_serving_request(
            a_request(shape=ShapeSchema(device_class="h100", device_count=6), parallelism=ParallelismSchema(tensor=4)),
            artifact,
        )
    with pytest.raises(Invalid):
        check_serving_request(a_request(cache=CacheSchema(kv_cache_fraction=1.5)), artifact)
    with pytest.raises(Invalid):
        check_serving_request(a_request(resources=ResourcesSchema(cpu_cores=-1)), artifact)


# --------------------------------------------------------------------------- the typed serving block


def test_a_workload_with_no_serving_block_still_round_trips():
    """Every row written before the block existed reads back as the defaults."""
    for stored in ({}, None):
        block = rendered(a_workload(serving=stored, capabilities=stored))
        assert block.serving == ServingSchema()
        assert block.capabilities == CapabilitiesSchema()
        assert block.serving.reasoning.parser is None
        assert block.serving.context_overflow == "reject"
        assert block.capabilities.input_modalities == ["text"]
        decoded = msgspec.json.decode(msgspec.json.encode(block.serving), type=ServingSchema)
        assert decoded == block.serving


def test_an_absent_serving_block_is_the_defaults_at_the_request_boundary_too():
    data = a_request()
    assert data.serving == ServingSchema()
    assert data.capabilities == CapabilitiesSchema()
    assert check_serving(data.serving, data.capabilities, prefix_cache_enabled=False) is None


def test_a_serving_block_round_trips():
    declared = serving(
        reasoning={
            "default_on": True,
            "parser": "qwen3",
            "transport": "inline_tags",
            "think_begin_token": "<think>",
            "think_end_token": "</think>",
            "suppress_eos_until_think_end": True,
        },
        stream_usage="every_chunk",
        context_overflow="clamp_output",
        structured_output_backend="xgrammar",
        tool_call_parser="hermes",
        max_images_per_request=4,
        max_videos_per_request=1,
    )
    claimed = capabilities(
        reasoning_supported=True,
        structured_output_modes=["text", "json_object", "json_schema"],
        tool_calling_supported=True,
        tool_choice_modes=["auto", "required"],
        parallel_tool_calls=True,
        input_modalities=["text", "image", "video"],
        prompt_cache_mode="automatic",
        prompt_cache_ttl_seconds=300,
        asserted_by="operator",
        asserted_at=EPOCH,
        source_uri="https://example.invalid/model-card",
    )
    assert check_serving(declared, claimed, prefix_cache_enabled=True) is None

    block = rendered(a_workload(serving=msgspec.to_builtins(declared), capabilities=msgspec.to_builtins(claimed)))
    assert block.serving == declared
    assert block.capabilities == claimed

    decoded = msgspec.json.decode(msgspec.json.encode(block), type=type(block))
    assert decoded.serving == declared
    assert decoded.capabilities == claimed
    assert decoded.capabilities.asserted_at == EPOCH


def test_the_serving_block_is_typed_rather_than_another_free_dict():
    """The reason it exists at all: before it, this went into `engine_args`.

    The plane could then neither validate it nor say what it had asked for. A typed
    block that accepted an unknown key would be the same free dict wearing a name.
    """
    assert WorkloadServingSchema.__annotations__["serving"] is ServingSchema
    assert WorkloadServingSchema.__annotations__["capabilities"] is CapabilitiesSchema
    assert "reasoning" in ServingSchema.__struct_fields__
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"thinking": True}, ServingSchema)


def test_a_reasoning_parser_named_with_thinking_off_is_refused():
    """The contradiction: how it reasons, declared on a workload claiming it cannot."""
    with pytest.raises(Invalid):
        check_serving(
            serving(reasoning={"parser": "qwen3"}),
            capabilities(reasoning_supported=False),
            prefix_cache_enabled=False,
        )
    with pytest.raises(Invalid):
        check_serving(
            serving(reasoning={"default_on": True}),
            capabilities(reasoning_supported=False),
            prefix_cache_enabled=False,
        )
    assert (
        check_serving(
            serving(reasoning={"parser": "qwen3"}),
            capabilities(reasoning_supported=True),
            prefix_cache_enabled=False,
        )
        is None
    )


def test_inline_traces_without_the_tokens_a_client_would_parse_are_refused():
    with pytest.raises(Invalid):
        check_serving(
            serving(reasoning={"transport": "inline_tags", "think_begin_token": "<think>"}),
            capabilities(reasoning_supported=True),
            prefix_cache_enabled=False,
        )


def test_suppressing_eos_needs_the_token_it_suppresses_until():
    with pytest.raises(Invalid):
        check_serving(
            serving(reasoning={"suppress_eos_until_think_end": True}),
            capabilities(reasoning_supported=True),
            prefix_cache_enabled=False,
        )


def test_a_tool_parser_on_a_workload_that_does_not_claim_tool_calling_is_refused():
    with pytest.raises(Invalid):
        check_serving(
            serving(tool_call_parser="hermes"),
            capabilities(tool_calling_supported=False),
            prefix_cache_enabled=False,
        )


def test_tool_choice_modes_are_non_empty_exactly_when_tool_calling_is_supported():
    with pytest.raises(Invalid):
        check_serving(serving(), capabilities(tool_calling_supported=True), prefix_cache_enabled=False)
    with pytest.raises(Invalid):
        check_serving(serving(), capabilities(tool_choice_modes=["auto"]), prefix_cache_enabled=False)


def test_a_structured_output_backend_without_json_schema_among_the_modes_is_refused():
    with pytest.raises(Invalid):
        check_serving(
            serving(structured_output_backend="xgrammar"),
            capabilities(structured_output_modes=["text", "json_object"]),
            prefix_cache_enabled=False,
        )


@pytest.mark.parametrize(
    "block",
    [
        {"structured_output_modes": ["text", "yaml"]},
        {"structured_output_modes": ["text", "text"]},
        {"tool_calling_supported": True, "tool_choice_modes": ["auto", "sometimes"]},
        {"input_modalities": ["text", "smell"]},
        {"input_modalities": []},
        {"prompt_cache_mode": "sometimes"},
        {"prompt_cache_ttl_seconds": -1},
    ],
)
def test_an_unknown_or_repeated_member_of_a_claimed_set_is_refused(block):
    with pytest.raises(Invalid):
        check_serving(serving(), capabilities(**block), prefix_cache_enabled=True)


def test_a_modality_set_without_text_is_accepted():
    """A transcription model takes audio, and text is at most an optional prompt.

    The rule here used to be that text is always an input modality. It refused
    `openai/whisper-large-v3` outright, which is a claim about what engines exist
    dressed up as a validation, and this block is the one the plane holds no table
    for. What is left is the only thing that is always true: something goes in.
    """
    check_serving(serving(), capabilities(input_modalities=["audio"]), prefix_cache_enabled=False)
    check_serving(serving(), capabilities(input_modalities=["text", "audio"]), prefix_cache_enabled=False)


@pytest.mark.parametrize(
    "field_name,modality", [("max_images_per_request", "image"), ("max_videos_per_request", "video")]
)
def test_a_modality_limit_needs_its_own_modality_among_the_declared_ones(field_name, modality):
    with pytest.raises(Invalid):
        check_serving(serving(**{field_name: 4}), capabilities(), prefix_cache_enabled=False)
    with pytest.raises(Invalid):
        check_serving(
            serving(**{field_name: 0}),
            capabilities(input_modalities=["text", modality]),
            prefix_cache_enabled=False,
        )
    assert (
        check_serving(
            serving(**{field_name: 4}),
            capabilities(input_modalities=["text", modality]),
            prefix_cache_enabled=False,
        )
        is None
    )


def test_an_image_cap_does_not_stand_in_for_a_video_one():
    """The caps are separate declarations, and one modality never licenses the other."""
    with pytest.raises(Invalid):
        check_serving(
            serving(max_images_per_request=4, max_videos_per_request=1),
            capabilities(input_modalities=["text", "image"]),
            prefix_cache_enabled=False,
        )


@pytest.mark.parametrize(
    "field_name,modality", [("max_images_per_request", "image"), ("max_videos_per_request", "video")]
)
def test_a_declared_modality_needs_its_own_limit(field_name, modality):
    """The converse guard. An unset cap renders as zero, not as no limit.

    `--limit-mm-per-prompt` carries one map, and vLLM reads every modality missing
    from it as zero. So a workload that claims the modality and leaves the cap at
    `None` ships a process that refuses it, with nothing anywhere saying so.
    """
    with pytest.raises(Invalid):
        check_serving(
            serving(),
            capabilities(input_modalities=["text", modality]),
            prefix_cache_enabled=False,
        )
    assert (
        check_serving(
            serving(**{field_name: 1}),
            capabilities(input_modalities=["text", modality]),
            prefix_cache_enabled=False,
        )
        is None
    )


def test_a_video_claim_does_not_ride_on_the_image_cap():
    """The defect this closes: an image cap renders `{"image": N}`, and video is then zero."""
    with pytest.raises(Invalid):
        check_serving(
            serving(max_images_per_request=4),
            capabilities(input_modalities=["text", "image", "video"]),
            prefix_cache_enabled=False,
        )
    both = serving(max_images_per_request=4, max_videos_per_request=1)
    assert (
        check_serving(both, capabilities(input_modalities=["text", "image", "video"]), prefix_cache_enabled=False)
        is None
    )
    assert serving_args("vllm", both) == {"limit-mm-per-prompt": '{"image": 4, "video": 1}'}


def test_allowed_tools_is_a_declarable_tool_choice_mode():
    """OpenAI's `tool_choice: {"type": "allowed_tools", ...}`, which upstreams serve today."""
    assert (
        check_serving(
            serving(),
            capabilities(tool_calling_supported=True, tool_choice_modes=["auto", "allowed_tools"]),
            prefix_cache_enabled=False,
        )
        is None
    )


def test_a_prompt_cache_contract_needs_the_mechanism_under_it():
    """The contract is a claim to a caller; `prefix_cache_enabled` is the flag that makes it true."""
    with pytest.raises(Invalid):
        check_serving(serving(), capabilities(prompt_cache_mode="explicit"), prefix_cache_enabled=False)
    assert check_serving(serving(), capabilities(prompt_cache_mode="explicit"), prefix_cache_enabled=True) is None


@pytest.mark.parametrize(
    "block",
    [{"stream_usage": "sometimes"}, {"context_overflow": "shrug"}, {"reasoning": {"transport": "telepathy"}}],
)
def test_an_unknown_member_of_a_rendered_enum_is_refused(block):
    with pytest.raises(Invalid):
        check_serving(serving(**block), capabilities(reasoning_supported=True), prefix_cache_enabled=False)


# --------------------------------------------------------------------------- resolved arguments


def test_a_typed_serving_field_beats_the_same_flag_in_engine_args():
    """The escape hatch keeps the flags nothing models, and loses the ones it does."""
    workload = a_workload(
        engine_args={"--reasoning-parser": "deepseek_r1", "max-num-seqs": "64"},
        serving=msgspec.to_builtins(serving(reasoning={"parser": "qwen3"})),
    )
    block = rendered(workload)
    assert block.resolved_engine_args["reasoning-parser"] == "qwen3"
    assert block.resolved_engine_args["max-num-seqs"] == "64"
    assert block.engine_args == workload.engine_args


def test_engine_args_keeps_every_flag_the_serving_block_does_not_model():
    block = rendered(a_workload(engine_args={"--enforce-eager": "", "swap-space": "8"}))
    assert block.engine_args == {"--enforce-eager": "", "swap-space": "8"}
    assert block.resolved_engine_args == {"enforce-eager": "", "swap-space": "8"}


def test_the_flag_a_typed_field_speaks_for_is_the_engines_own_spelling():
    block = serving(structured_output_backend="xgrammar")
    assert serving_args("sglang", block) == {"grammar-backend": "xgrammar"}
    assert serving_args("trtllm", block) == {}
    assert serving_args("vllm", serving(max_images_per_request=4)) == {"limit-mm-per-prompt": '{"image": 4}'}
    assert serving_args("vllm", serving(max_videos_per_request=1)) == {"limit-mm-per-prompt": '{"video": 1}'}


def test_two_modality_caps_render_as_one_map_and_not_as_two_flags():
    """`--limit-mm-per-prompt` is one argument holding every modality it limits.

    Assigning it per field would leave whichever cap rendered second, and a workload that
    declared both would silently serve one of them uncapped.
    """
    both = serving(max_images_per_request=4, max_videos_per_request=1)
    assert serving_args("vllm", both) == {"limit-mm-per-prompt": '{"image": 4, "video": 1}'}


def test_an_image_cap_is_json_and_arrives_with_the_switch_that_turns_images_on():
    """Both halves were wrong, and each one alone is a worker that does not serve images.

    `--limit-mm-per-prompt` on this engine takes JSON. Rendered as `image=4` the worker
    exits on `Value image=4 cannot be converted to <function loads>` before it loads a
    weight — a crashloop for every workload that declared a cap. And a cap on its own
    caps a path that is off: `--enable-multimodal` is what turns the multimodal
    components on at all, so without it the flag that survived would have limited
    nothing.
    """
    workload = a_workload(serving=block(max_images_per_request=4))
    resolved = resolved_engine_args(workload, an_artifact())
    assert resolved["limit-mm-per-prompt"] == '{"image": 4}'
    assert resolved["enable-multimodal"] == ""


def test_a_video_cap_turns_the_same_switch_on():
    """`--enable-multimodal` is per worker, not per modality: video needs it exactly as image does."""
    workload = a_workload(serving=block(max_videos_per_request=1))
    resolved = resolved_engine_args(workload, an_artifact())
    assert resolved["limit-mm-per-prompt"] == '{"video": 1}'
    assert resolved["enable-multimodal"] == ""


def test_the_switch_is_inferred_so_a_workload_that_states_it_by_hand_wins():
    """A companion flag loses to `engine_args`, which is what makes it safe to infer.

    An engine build wanting a value there, or this table getting the spelling wrong, is
    a thing one workload can hold against the plane without an edit here.
    """
    workload = a_workload(
        engine_args={"--enable-multimodal": "true"},
        serving=block(max_images_per_request=4),
    )
    assert resolved_engine_args(workload, an_artifact())["enable-multimodal"] == "true"


def test_a_workload_that_asks_for_no_attachments_gets_neither_flag():
    """The pair is additive: every workload already running is untouched by both."""
    resolved = resolved_engine_args(a_workload(), an_artifact())
    assert "enable-multimodal" not in resolved
    assert "limit-mm-per-prompt" not in resolved


def test_a_vllm_tool_parser_renders_no_flag_because_dynamo_takes_none():
    """The flags belong to `vllm serve`, and Dynamo does not run `vllm serve`.

    It runs `dynamo.vllm` for the engine and `dynamo.frontend` for the OpenAI
    surface, and `--tool-call-parser` and `--enable-auto-tool-choice` are in the
    `--help` of neither. Rendered onto the worker they were not a misconfiguration
    that degraded tool calling: the process exited on `unrecognized arguments`
    before it loaded a weight, on every restart, for every workload naming a parser.

    The field is still stored and still read back. What it no longer does is put a
    flag on a command line that has never had one.
    """
    assert resolved_engine_args(a_workload(serving=block(tool_call_parser="openai")), an_artifact()) == {}
    assert serving_args("vllm", serving(structured_output_backend="xgrammar")) == {}


def test_only_the_engines_that_need_the_companion_flag_get_it():
    """SGLang runs its own server, whose CLI does carry the flag this one names."""
    workload = a_workload(engine="sglang", serving=block(tool_call_parser="qwen25"))
    assert resolved_engine_args(workload, an_artifact()) == {"tool-call-parser": "qwen25"}
    assert serving_args("vllm", serving(reasoning={"parser": "qwen3"})) == {"reasoning-parser": "qwen3"}


def test_engine_args_still_carries_a_flag_this_table_no_longer_renders():
    """`engine_args` stays the escape hatch, and is the only route these flags now have.

    Somebody serving a build that really does take them can still say so by hand, and
    that is the correct place for it: a flag typed into `engine_args` is a claim about
    one workload's image, where a row in `SERVING_FLAGS` is a claim about every image
    the engine name covers.
    """
    resolved = resolved_engine_args(
        a_workload(
            engine_args={"--enable-auto-tool-choice": "true", "--tool-call-parser": "openai"},
            serving=block(tool_call_parser="openai"),
        ),
        an_artifact(),
    )
    assert resolved["tool-call-parser"] == "openai"
    assert resolved["enable-auto-tool-choice"] == "true"


def test_a_default_serving_block_speaks_for_no_flag_at_all():
    """Which is what keeps this additive on every workload that already exists."""
    assert serving_args("vllm", ServingSchema()) == {}
    workload = a_workload(engine_args={"--dtype": "bfloat16"})
    assert resolved_engine_args(workload, an_artifact()) == {"dtype": "bfloat16"}


# --------------------------------------------------------------------------- capacity


def artifact_row(*, parameter_count: int | None, quantization: str = "fp16", with_shape: bool = True):
    shape_of = extract_shape(
        {
            "num_hidden_layers": 32,
            "num_attention_heads": 32,
            "num_key_value_heads": 8,
            "hidden_size": 4096,
            "max_position_embeddings": 8192,
        }
    )
    assert shape_of is not None
    metadata = {"quantization": quantization}
    if with_shape:
        metadata["shape"] = shape_metadata(shape_of)
    return SimpleNamespace(parameter_count=parameter_count, metadata_=metadata)


def test_a_shape_that_cannot_hold_the_weights_is_refused():
    """The arithmetic half of a fit: 130 GB of weights do not go on one 80 GB card."""
    sizing = sizing_for(artifact_row(parameter_count=70_000_000_000), "fp16")
    with pytest.raises(Invalid):
        check_capacity(sizing, "h100-80gb-sxm", 1, 1)
    assert check_capacity(sizing, "h100-80gb-sxm", 4, 4) is None


def test_the_workloads_quantization_decides_what_has_to_be_resident():
    """Serving fp16 weights at fp8 halves the footprint, and the workload owns that call."""
    artifact = artifact_row(parameter_count=70_000_000_000, quantization="fp16")
    with pytest.raises(Invalid):
        check_capacity(sizing_for(artifact, "fp16"), "h100-80gb-sxm", 1, 1)
    assert check_capacity(sizing_for(artifact, "int4"), "h100-80gb-sxm", 1, 1) is None


def test_a_capacity_check_is_silent_about_what_it_cannot_measure():
    """An unsizeable artifact or an unknown class is not grounds to refuse a workload."""
    assert check_capacity(None, "h100-80gb-sxm", 1, 1) is None
    assert check_capacity(sizing_for(artifact_row(parameter_count=None), "fp16"), "h100-80gb-sxm", 1, 1) is None
    assert (
        check_capacity(sizing_for(artifact_row(parameter_count=70_000_000_000, with_shape=False), "fp16"), "h100", 1, 1)
        is None
    )
    sizing = sizing_for(artifact_row(parameter_count=405_000_000_000), "fp16")
    assert check_capacity(sizing, "some-card-the-table-has-never-heard-of", 1, 1) is None


def test_a_floor_no_card_of_the_declared_class_can_meet_is_refused():
    """The failure this check exists for.

    A workload asked for `h200-141gb-sxm` with a floor of 192 GiB per device. Every
    H200 in the fleet has 141, so placement filtered all eight out before it looked at
    which were free, reported `no_such_device_class`, and the workload expanded to no
    deployment at all — while the plane's own capacity check had already agreed that
    four of those cards can serve the model.
    """
    with pytest.raises(Invalid) as raised:
        check_floor("h200-141gb-sxm", 196_608)
    assert "196608" in str(raised.value)
    assert check_floor("h200-141gb-sxm", 143_771) is None
    assert check_floor("h200-141gb-sxm", 98_304) is None


def test_a_floor_check_is_silent_about_what_it_cannot_measure():
    """No floor, and a class the catalogue does not name, are both left alone."""
    assert check_floor("h200-141gb-sxm", 0) is None
    assert check_floor("some-card-the-table-has-never-heard-of", 1_000_000) is None


def test_a_context_window_that_does_not_fit_is_not_a_refusal():
    """The estimate has a 15% fudge in it, so enforcing it would refuse working workloads."""
    sizing = sizing_for(artifact_row(parameter_count=70_000_000_000), "fp16")
    assert check_capacity(sizing, "h100-80gb-sxm", 2, 2) is None


# --------------------------------------------------------------------------- images


def test_the_runtime_image_is_resolved_from_the_engine_and_the_runtime_version():
    """The common case: an author picks an engine, not a registry address."""
    assert resolve_engine_runtime_image("vllm", "1.4.1", None) == "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1"


def test_a_declared_runtime_image_always_wins():
    """Pinning an exact image is the reason the field is writable."""
    assert resolve_engine_runtime_image("vllm", "1.4.1", "my.registry/vllm:dev") == "my.registry/vllm:dev"


def test_latest_resolves_to_a_real_tag():
    """`latest` is not a tag these registries publish; taking it verbatim is a pull failure."""
    resolved = resolve_engine_runtime_image("vllm", "latest", None)
    assert resolved is not None
    assert resolved.endswith(f":{settings.ENGINE_RUNTIME_TAG_DEFAULT}")


def test_an_empty_version_resolves_to_a_real_tag():
    resolved = resolve_engine_runtime_image("vllm", "", None)
    assert resolved is not None
    assert resolved.endswith(f":{settings.ENGINE_RUNTIME_TAG_DEFAULT}")


def test_an_unknown_engine_has_no_default():
    """Inventing an address would trade a visible gap for an ImagePullBackOff."""
    assert resolve_engine_runtime_image("some-engine-nobody-configured", "1.0", None) is None


def test_the_engine_name_is_matched_case_insensitively():
    assert resolve_engine_runtime_image("VLLM", "1.4.1", None) is not None


def test_a_malformed_repo_pair_does_not_break_the_write_path():
    """One bad character in an env var must not be why a workload cannot be created."""
    original = settings.ENGINE_RUNTIME_REPOS
    settings.ENGINE_RUNTIME_REPOS = "garbage,,vllm=repo/vllm"
    try:
        assert resolve_engine_runtime_image("vllm", "1.4.1", None) == "repo/vllm:1.4.1"
    finally:
        settings.ENGINE_RUNTIME_REPOS = original


def test_a_declared_frontend_image_is_carried_verbatim():
    """There is no `resolve_frontend_image`, and the absence is the design.

    The plane knows which repository an engine's workers come from because
    `ENGINE_RUNTIME_REPOS` tells it. Nothing tells it what a cluster serves its
    frontends through — that is the operator's fact — so the only frontend image
    this side can state is one a person pinned.
    """
    pinned = "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2"
    assert rendered(a_workload(frontend_image=pinned)).frontend_image == pinned


def test_a_workload_naming_no_frontend_image_does_not_borrow_the_engines():
    """Empty means the cluster decides, and it must not quietly mean `engine_runtime_image`.

    Kimi K3 is the case the field exists for and it is the case where the two
    differ: the frontend carries the tokenizer fix, the worker stays on the base
    image. A read path that filled the gap with the engine image would report the
    one answer that is wrong precisely when it matters.
    """
    engine = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.0-patched.1"
    block = rendered(a_workload(engine_runtime_image=engine))
    assert block.engine_runtime_image == engine
    assert block.frontend_image is None


# --------------------------------------------------------------------------- the wire


def test_a_capability_claim_never_reaches_a_cluster():
    """A cluster has no use for a claim about itself, exactly as with `imported_args`."""
    assert "capabilities" not in ServingSpec.__struct_fields__
    assert "imported_args" not in ServingSpec.__struct_fields__


def test_the_serving_spec_carries_no_identity_and_no_routing():
    """It is inlined on the deployment, so every field naming an object is the deployment's.

    `RecipeDesired` carried its own id, the cluster it was sent to, the artifact it
    served, a name, a version and labels. None of that describes a process, and each
    of them was a second place for the deployment's answer to disagree.
    """
    fields = set(ServingSpec.__struct_fields__)
    assert not fields & {"recipe_id", "cluster_id", "artifact_id", "name", "version", "labels", "updated_at"}
    assert not fields & {"served_model_name", "device_class", "devices_per_replica"}
    assert "digest" in fields


def test_the_typed_serving_block_reaches_the_cluster():
    """The flags a workload declares must be the flags the engine is started with.

    `ServingSpec` carries no typed block — the operator renders a command line and
    has no use for a typed one — so the resolved arguments are what goes on the
    wire. Sending `engine_args` raw instead made the whole serving block a view: the
    console rendered `--reasoning-parser` and no cluster was ever told.
    `libs/conformance` found it that way round, against a live endpoint whose
    configuration read as configured and whose engine had never heard of the flag.
    """
    workload = a_workload(
        engine_runtime_version="0.10.0",
        engine_args={"--async-scheduling": ""},
        serving={"reasoning": {"parser": "qwen3"}},
        tensor_parallel=4,
        device_count=4,
        context_length=131072,
        prefix_cache_enabled=True,
        serving_digest="svg_" + "a" * 64,
    )

    spec = _serving(workload, an_artifact())

    assert spec.engine_args == {
        "async-scheduling": "",
        "reasoning-parser": "qwen3",
    }
    assert spec.digest == workload.serving_digest


# --------------------------------------------------------------------------- the digest


def test_renaming_a_column_does_not_move_the_identity_of_a_configuration():
    """`engine_runtime_version` still hashes under `engine_version`, and must for ever.

    `ops_benchmark_run.serving_digest` names the configuration a measurement was taken
    against, so a digest that shifted when a column was renamed would leave every stored
    run pointing at a configuration no workload has — a benchmark attributed to nothing,
    failing silently. `DIGEST_FIELDS` is `(hash key, attribute)` for exactly this: the
    attribute half follows the code, the key half never moves.

    The literal below is the digest of the block in `a_workload()` and is checked against
    the payload assembled the way the pre-rename code assembled it, so this fails if
    anyone re-points a hash key at its new column name.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    workload = a_workload()

    payload = {"artifact": str(artifact.uid)}
    for key, attribute in DIGEST_FIELDS:
        payload[key] = _canonical(getattr(workload, attribute))
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = "svg_" + hashlib.sha256(body.encode("utf-8")).hexdigest()

    assert serving_digest(workload, artifact, ()) == expected
    assert [key for key, _ in DIGEST_FIELDS][:3] == ["engine", "engine_version", "engine_image"]
    assert [attr for _, attr in DIGEST_FIELDS][:3] == [
        "engine",
        "engine_runtime_version",
        "engine_runtime_image",
    ]


def test_two_workloads_asking_for_the_same_thing_carry_one_digest():
    """The identity of a serving configuration is its content, stated literally.

    `matchExistingRecipe` compared eight fields in a browser to decide whether an
    existing recipe could be reused, and got it wrong whenever a ninth had been
    hand-tuned. Comparing contents to find an object is the tell that the contents
    are the object.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    one, other = a_workload(), a_workload(name="a-different-name")

    assert serving_digest(one, artifact, ()) == serving_digest(other, artifact, ())
    assert serving_digest(one, artifact, ()).startswith("svg_")
    assert len(serving_digest(one, artifact, ())) == len("svg_") + 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("engine", "sglang"),
        ("engine_runtime_version", "0.7.0"),
        ("engine_runtime_image", "my.registry/vllm:dev"),
        ("frontend_image", "my.registry/frontend:dev"),
        ("engine_args", {"--enforce-eager": ""}),
        ("quantization", "fp8"),
        ("dtype", "bfloat16"),
        ("tensor_parallel", 2),
        ("pipeline_parallel", 2),
        ("expert_parallel", 2),
        ("context_length", 4096),
        ("max_batch_tokens", 8192),
        ("kv_cache_dtype", "fp8"),
        ("kv_cache_fraction", Decimal("0.9000")),
        ("prefix_cache_enabled", True),
        ("prefix_cache_config", {"mode": "kv"}),
        ("device_class", "h200"),
        ("device_count", 2),
        ("device_memory_mib_min", 40960),
        ("cpu_cores", 8),
        ("memory_mib", 65536),
        ("disk_gib", 100),
        ("overlay", {"dynamo": {"replicas": 2}}),
        ("serving", {"tool_call_parser": "openai"}),
    ],
)
def test_every_field_that_decides_what_process_runs_moves_the_digest(field, value):
    """The digest answers "would this produce the same running process".

    Parametrised over the whole list rather than spot-checked, because a field
    quietly dropped from it is a digest that says two different engines are the
    same configuration — and nothing downstream would notice until a benchmark
    result was attributed to the wrong fit.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    assert serving_digest(a_workload(**{field: value}), artifact, ()) != serving_digest(a_workload(), artifact, ())


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "something-else"),
        ("labels", {"tier": "gold"}),
        ("imported_args", {"--max-num-seqs": "256"}),
        ("capabilities", {"tool_calling_supported": True}),
    ],
)
def test_what_is_not_a_footprint_is_not_in_the_digest(field, value):
    """The absences are the point of the field.

    `imported_args` was read off somebody else's running process and is never
    rendered; `capabilities` is asserted and never reaches a cluster; a name is a
    name. Two workloads differing only in these ask for the same engine.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    assert serving_digest(a_workload(**{field: value}), artifact, ()) == serving_digest(a_workload(), artifact, ())


def test_the_weights_are_part_of_the_configuration():
    """Same engine, same shape, different model is not the same running process."""
    workload = a_workload()
    assert serving_digest(workload, Artifact(uid=new_uid(ARTIFACT)), ()) != serving_digest(
        workload, Artifact(uid=new_uid(ARTIFACT)), ()
    )


def test_a_fraction_is_quantised_on_the_way_in_so_a_reload_hashes_the_same():
    """A create and the next read of the same row must not be two configurations.

    `kv_cache_fraction` is `Numeric(5, 4)`, so a float written straight through comes
    back as `Decimal("0.9000")`, and the digest hashes the column as the string it
    round-trips as. Without quantising on the way in, the digest a create returned
    and the digest the next write computed would differ for a row nobody touched —
    one configuration with two identities, produced by nothing but a reload.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    written = a_workload()
    apply_serving(written, a_request(cache=CacheSchema(kv_cache_fraction=0.9)), artifact, ())
    assert written.kv_cache_fraction == Decimal("0.9000")

    reloaded = a_workload()
    apply_serving(reloaded, a_request(cache=CacheSchema(kv_cache_fraction=0.9)), artifact, ())
    reloaded.kv_cache_fraction = Decimal("0.9000")
    assert serving_digest(reloaded, artifact, ()) == written.serving_digest

    unquantised = a_workload()
    apply_serving(unquantised, a_request(cache=CacheSchema(kv_cache_fraction=0.9)), artifact, ())
    unquantised.kv_cache_fraction = 0.9
    assert serving_digest(unquantised, artifact, ()) != written.serving_digest


def test_the_digest_is_read_off_the_row_and_never_off_the_request():
    """What is hashed is what was stored, coercions included.

    A digest computed from the request would name a configuration nobody is running
    the moment the write path coerces anything — which it does, on every fraction.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    workload = a_workload()
    apply_serving(workload, a_request(engine_runtime_version="0.7.0"), artifact, ())

    assert workload.engine_runtime_version == "0.7.0"
    assert workload.serving_digest == serving_digest(workload, artifact, ())


def test_the_slug_of_the_weights_is_what_the_engine_answers_to():
    """A fact about the weights, which survives a re-fit.

    It used to be `Recipe.name` — `kimi-k3-mxfp4-tp8-1024k` — so re-fitting the same
    model at a different tensor degree renamed the model every client codes against.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT), name="Kimi-K3", slug="moonshotai/kimi-k3")
    assert served_model(a_workload(), artifact) == "moonshotai/kimi-k3"


def test_the_engine_args_hatch_still_beats_what_the_plane_would_say():
    """It is laid over the derived flags in the render, so the plane must agree with it."""
    artifact = Artifact(uid=new_uid(ARTIFACT), name="Kimi-K3", slug="moonshotai/kimi-k3")
    workload = a_workload(engine_args={"--served-model-name": "acme/kimi-canary"})
    assert served_model(workload, artifact) == "acme/kimi-canary"


def test_a_recipe_default_moves_the_digest():
    """A default that changes the command line has to change the identity of the configuration.

    The digest is what the operator compares to decide whether the graph it is
    already running was built from these inputs, and it skips the apply when the
    answer is yes. So a recipe default that reaches the wire without reaching the
    digest is a change the cluster is told about and can never act on: on site-b the
    desired state carried `max-num-seqs 32` for GLM-5.3-Flash while the worker went
    on crash-looping under the old command line, and the operator logged `already
    built from these inputs; not applying` every fifteen seconds over the top of it.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT), slug="zai-org/GLM-5.3-Flash")
    workload = a_workload(device_class="h200-141gb-sxm")

    assert recipe_engine_args(workload, artifact).get("max-num-seqs") == "32"
    assert serving_digest(workload, artifact, ()) != serving_digest(workload, Artifact(uid=artifact.uid), ())


def test_a_model_with_no_recipe_hashes_exactly_as_it_did():
    """And the other half: nothing else in the fleet may move.

    An absent key and an empty map are different hashes. If this function wrote
    `engine_defaults: {}` for every model the corpus does not cover, every workload
    in the fleet would take a new digest at once and every deployment would
    re-render — which is the fleet-wide reload the digest exists to prevent.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT), slug="nobody/not-in-the-corpus")
    workload = a_workload(device_class="h200-141gb-sxm")

    assert recipe_engine_args(workload, artifact) == {}

    payload = {"artifact": str(artifact.uid)}
    for key, attribute in DIGEST_FIELDS:
        payload[key] = _canonical(getattr(workload, attribute))
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert serving_digest(workload, artifact, ()) == "svg_" + hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- the disaggregated digest


def a_role(role: str, **kwargs) -> WorkloadRole:
    base = {"role": role, "weight": 1, "max_batch_tokens": None, "engine_args": {}}
    return WorkloadRole(**{**base, **kwargs})


def test_an_aggregated_workload_hashes_exactly_as_it_did_before_roles_existed():
    """The pin that makes the migration additive, and the reason the fields are conditional.

    Every workload in the fleet is aggregated. If `serving_topology` or the role block reached
    the payload for them, every digest would move at migration — every deployment's
    `ServingDigestAnnotation` with it, and every `ops_benchmark_run` would lose the link back to
    the configuration it measured. The literal is assembled the way the pre-roles code assembled
    it, so this fails if anyone makes either field unconditional.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    workload = a_workload()

    payload = {"artifact": str(artifact.uid)}
    for key, attribute in DIGEST_FIELDS:
        payload[key] = _canonical(getattr(workload, attribute))
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert serving_digest(workload, artifact, ()) == "svg_" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert "serving_topology" not in body
    assert "roles" not in body


def test_a_ratio_change_does_not_change_the_identity_of_the_configuration():
    """1:2 and 2:4 are the same configuration served at two scales, and one digest.

    Not because a digest change would restart anything — it would not; every scale in the system
    already bumps the generation and re-applies. Because `ops_benchmark_run.serving_digest` is
    what links a measurement to what it measured, and a weight in the hash would orphan every
    benchmark on a model the moment somebody added a prefill worker. Those benchmarks are the
    evidence anyone would use to choose the ratio in the first place.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    workload = a_workload(serving_topology="disaggregated")

    one_two = [a_role("prefill", weight=1), a_role("decode", weight=2)]
    two_four = [a_role("prefill", weight=2), a_role("decode", weight=4)]

    assert serving_digest(workload, artifact, one_two) == serving_digest(workload, artifact, two_four)


def test_the_role_order_they_arrive_in_is_not_part_of_the_configuration():
    artifact = Artifact(uid=new_uid(ARTIFACT))
    workload = a_workload(serving_topology="disaggregated")
    roles = [a_role("prefill"), a_role("decode")]

    assert serving_digest(workload, artifact, roles) == serving_digest(workload, artifact, list(reversed(roles)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_batch_tokens", 32768),
        ("engine_args", {"max-num-seqs": "4"}),
    ],
)
def test_a_role_argument_is_configuration_and_moves_the_digest(field, value):
    """Prefill at 32768 batched tokens and decode at 2048 are not the same process.

    A sixteenfold difference in how much work one engine step does is exactly what the digest
    is for. This is the other half of the weights being excluded: what a role *is* belongs in
    the hash, how many of it there are does not.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    workload = a_workload(serving_topology="disaggregated")
    plain = [a_role("prefill"), a_role("decode")]
    changed = [a_role("prefill", **{field: value}), a_role("decode")]

    assert serving_digest(workload, artifact, plain) != serving_digest(workload, artifact, changed)


def test_disaggregating_is_a_configuration_change_and_aggregating_back_returns_the_same_digest():
    artifact = Artifact(uid=new_uid(ARTIFACT))
    aggregated = a_workload()
    disaggregated = a_workload(serving_topology="disaggregated")
    roles = [a_role("prefill"), a_role("decode", weight=2)]

    assert serving_digest(aggregated, artifact, ()) != serving_digest(disaggregated, artifact, roles)
    assert serving_digest(aggregated, artifact, ()) == serving_digest(a_workload(), artifact, ())


def test_device_count_is_per_worker_and_the_group_total_is_not_a_column():
    """The side door the weights would otherwise come back through.

    `device_count` *is* a digest field. If it ever held the group's 24 devices rather than one
    worker's 8, the weights would be in the hash again — silently, because nothing about the
    column's name says which of the two it holds. So the group's cost is `Σ (weight × device_count)`,
    derived where it is used and stored nowhere, and these two assertions are what say so: the
    weights move nothing, and the per-worker count moves everything.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    workload = a_workload(serving_topology="disaggregated", device_count=8, tensor_parallel=8)
    roles = [a_role("prefill", weight=1), a_role("decode", weight=2)]

    assert serving_digest(workload, artifact, roles) == serving_digest(
        workload, artifact, [a_role("prefill", weight=3), a_role("decode", weight=9)]
    )
    group = a_workload(serving_topology="disaggregated", device_count=24, tensor_parallel=8)
    assert serving_digest(group, artifact, roles) != serving_digest(workload, artifact, roles)


# ------------------------------------------------------------- refusing a bad topology


def test_an_aggregated_workload_may_not_carry_roles():
    """Silently ignoring them would let somebody believe they had declared a ratio."""
    with pytest.raises(Invalid):
        check_topology("aggregated", [a_role("prefill"), a_role("decode")], "vllm")

    assert check_topology("aggregated", (), "vllm") is None


@pytest.mark.parametrize(
    "roles",
    [
        [],
        [a_role("prefill")],
        [a_role("decode")],
        [a_role("prefill"), a_role("worker")],
    ],
)
def test_a_disaggregated_workload_is_exactly_two_roles(roles):
    """One of them is not a topology, and a name outside the set renders a mode no engine takes."""
    with pytest.raises(Invalid):
        check_topology("disaggregated", roles, "vllm")


def test_a_role_with_no_workers_is_not_a_role():
    with pytest.raises(Invalid):
        check_topology("disaggregated", [a_role("prefill", weight=0), a_role("decode")], "vllm")


def test_any_pair_of_positive_weights_is_a_legitimate_ratio():
    """`2:2` says two of each. Coprimality was dropped with the argument that required it.

    It existed so that one footprint had one spelling in the digest — but the weights are not in
    the digest at all, so all it bought was making "two of each" inexpressible as such.
    """
    for prefill, decode in ((1, 2), (2, 2), (3, 1), (2, 4)):
        assert (
            check_topology(
                "disaggregated", [a_role("prefill", weight=prefill), a_role("decode", weight=decode)], "vllm"
            )
            is None
        )


@pytest.mark.parametrize(
    "flag", ["max-num-batched-tokens", "--tensor-parallel-size", "disaggregation-mode", "kv-transfer-config"]
)
def test_a_role_may_not_contradict_a_flag_the_plane_already_decided(flag):
    """Two sources: a typed column somebody filled in, and the topology itself.

    `kv-transfer-config` is the same string on both roles because the transfer mechanism belongs
    to the topology, and `disaggregation-mode` *is* the role. A value for either from a role's
    own arguments is a second answer to a settled question, and `engine_args` wins on the command
    line — so it would not be overruled, it would win.
    """
    roles = [a_role("prefill", engine_args={flag: "1"}), a_role("decode")]
    with pytest.raises(Invalid):
        check_topology("disaggregated", roles, "vllm")


def test_an_unrecognised_topology_is_refused_rather_than_defaulted():
    with pytest.raises(Invalid):
        check_topology("hybrid", (), "vllm")


# ------------------------------------------------------- the declared topology surface


def test_a_group_costs_the_sum_of_its_roles_and_device_count_stays_per_worker():
    """The arithmetic that must never become a column.

    `device_count` is a digest field and means devices per worker. The group's cost is derived
    wherever it is used and stored nowhere, because a column holding it would put the role weights
    into `serving_digest` through a second door — silently, since nothing about such a column's
    name would say which of the two it held.
    """
    roles = [a_role("prefill", weight=1), a_role("decode", weight=2)]
    assert group_devices(8, roles) == 24
    assert group_devices(8, [a_role("prefill", weight=2), a_role("decode", weight=2)]) == 32
    # An aggregated workload's group is one worker, so the same function answers for both.
    assert group_devices(8, []) == 8


def test_the_topology_is_refused_as_part_of_the_whole_serving_request():
    """`check_topology` is reached through the one entry point create and update share.

    An update is validated as the whole configuration it would leave behind, so a patch cannot
    arrive at a topology a create would have refused — which is the property `check_serving_request`
    exists for, and it only holds if the topology check is inside it.
    """
    artifact = an_artifact()
    bad = a_request()
    bad.topology = TopologySchema(serving_topology="disaggregated", roles=[RoleSchema(role="prefill")])
    with pytest.raises(Invalid):
        check_serving_request(bad, artifact)

    good = a_request()
    good.topology = TopologySchema(
        serving_topology="disaggregated",
        roles=[RoleSchema(role="prefill", weight=1), RoleSchema(role="decode", weight=2)],
    )
    assert check_serving_request(good, artifact) is None


@pytest.mark.parametrize(
    "flag", ["enable-log-requests", "--enable-log-outputs", "enable_log_requests", "log-requests", "log-requests-level"]
)
def test_an_engine_flag_that_logs_request_content_is_refused(flag):
    """Zero retention: no workload may turn on the engine's prompt or completion logging."""
    with pytest.raises(Invalid) as refused:
        check_serving_request(a_request(engine_args={flag: ""}), an_artifact())
    assert refused.value.detail["flag"] == flag.lstrip("-").replace("_", "-")


def test_a_role_cannot_turn_on_request_content_logging_either():
    request = a_request()
    request.topology = TopologySchema(
        serving_topology="disaggregated",
        roles=[
            RoleSchema(role="prefill", weight=1),
            RoleSchema(role="decode", weight=2, engine_args={"enable-log-outputs": ""}),
        ],
    )
    with pytest.raises(Invalid):
        check_serving_request(request, an_artifact())


@pytest.mark.parametrize(
    "overlay",
    [
        OverlaySchema(pod_spec={"containers": [{"name": "main", "args": ["--enable-log-requests"]}]}),
        OverlaySchema(
            pod_spec={"containers": [{"name": "main", "command": ["sh", "-c", "vllm serve --enable-log-outputs"]}]}
        ),
        OverlaySchema(
            dynamo={"spec": {"services": {"worker": {"extraPodSpec": {"args": ["--log-requests-level=2"]}}}}}
        ),
    ],
)
def test_an_overlay_cannot_turn_on_request_content_logging(overlay):
    """The overlay merges after the operator filters engine args, so the plane is the only check on it."""
    with pytest.raises(Invalid):
        check_serving_request(a_request(overlay=overlay), an_artifact())


def test_an_overlay_without_content_logging_is_untouched():
    overlay = OverlaySchema(pod_spec={"containers": [{"name": "main", "args": ["--no-enable-log-requests", "-v"]}]})
    assert check_serving_request(a_request(overlay=overlay), an_artifact()) is None


@pytest.mark.parametrize("level", ["debug", "DEBUG", "trace", "10"])
def test_an_engine_log_level_that_logs_request_content_is_refused(level):
    """SGLang's detectors log completion text at debug, so the level is a content-logging switch too."""
    with pytest.raises(Invalid) as refused:
        check_serving_request(a_request(engine_args={"--log_level": level}), an_artifact())
    assert refused.value.detail["flag"] == "log-level"


def test_a_role_cannot_raise_its_log_level_to_debug_either():
    request = a_request()
    request.topology = TopologySchema(
        serving_topology="disaggregated",
        roles=[
            RoleSchema(role="prefill", weight=1, engine_args={"log-level": "debug"}),
            RoleSchema(role="decode", weight=2),
        ],
    )
    with pytest.raises(Invalid):
        check_serving_request(request, an_artifact())


@pytest.mark.parametrize("level", ["info", "warning", "error", "20"])
def test_an_engine_log_level_that_logs_no_request_content_is_allowed(level):
    assert check_serving_request(a_request(engine_args={"log-level": level}), an_artifact()) is None


def env_overlay(entry: dict, where: str = "pod_spec") -> OverlaySchema:
    if where == "pod_spec":
        return OverlaySchema(pod_spec={"containers": [{"name": "main", "env": [entry]}]})
    return OverlaySchema(dynamo={"spec": {"components": {"VllmDecodeWorker": {"envs": [entry]}}}})


@pytest.mark.parametrize("where", ["pod_spec", "dynamo"])
@pytest.mark.parametrize(
    "entry",
    [
        {"name": "DYN_LOG", "value": "debug"},
        {"name": "DYN_LOG", "value": "info,dynamo_llm::http=trace"},
        {"name": "DYN_LOGGING_CONFIG_PATH", "value": "/etc/dynamo/logging.toml"},
        {"name": "VLLM_LOGGING_LEVEL", "value": "DEBUG"},
        {"name": "VLLM_DEBUG_LOG_API_SERVER_RESPONSE", "value": "true"},
        {"name": "DYN_LOG", "valueFrom": {"configMapKeyRef": {"name": "logging", "key": "level"}}},
    ],
    ids=["dyn-debug", "dyn-target-trace", "dyn-config-file", "vllm-debug", "vllm-response-bodies", "value-from"],
)
def test_an_overlay_env_that_logs_request_content_is_refused(entry, where):
    """The overlay is the one place an environment variable reaches a worker or the frontend from."""
    with pytest.raises(Invalid) as refused:
        check_serving_request(a_request(overlay=env_overlay(entry, where)), an_artifact())
    assert refused.value.detail["env"] == entry["name"]


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "DYN_LOG", "value": "info,dynamo_llm=warn"},
        {"name": "VLLM_LOGGING_LEVEL", "value": "WARNING"},
        {"name": "VLLM_DEBUG_LOG_API_SERVER_RESPONSE", "value": "False"},
        {"name": "NCCL_DEBUG", "value": "INFO"},
        {"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": {"name": "hf", "key": "token"}}},
    ],
)
def test_an_overlay_env_that_logs_no_request_content_is_untouched(entry):
    assert check_serving_request(a_request(overlay=env_overlay(entry)), an_artifact()) is None


@pytest.mark.parametrize(
    "args",
    [["--log-level", "debug"], ["--log-level=DEBUG"], ["sh", "-c", "python -m dynamo.sglang --log-level debug"]],
)
def test_an_overlay_command_line_that_raises_the_log_level_is_refused(args):
    overlay = OverlaySchema(pod_spec={"containers": [{"name": "main", "args": args}]})
    with pytest.raises(Invalid) as refused:
        check_serving_request(a_request(overlay=overlay), an_artifact())
    assert refused.value.detail["flag"] == "log-level"


def test_an_overlay_command_line_at_info_is_untouched():
    overlay = OverlaySchema(pod_spec={"containers": [{"name": "main", "args": ["--log-level", "info"]}]})
    assert check_serving_request(a_request(overlay=overlay), an_artifact()) is None


def test_the_explicit_off_spelling_of_request_logging_is_allowed():
    assert check_serving_request(a_request(engine_args={"no-enable-log-requests": ""}), an_artifact()) is None


def test_an_aggregated_request_still_validates_with_no_topology_named():
    assert check_serving_request(a_request(), an_artifact()) is None


def test_the_declared_roles_are_what_gets_validated_stored_and_hashed():
    """One conversion, so the thing refused is the thing stored and the thing stored is hashed."""
    block = TopologySchema(
        serving_topology="disaggregated",
        roles=[
            RoleSchema(role="prefill", weight=1, max_batch_tokens=32768, engine_args={"max-num-seqs": "4"}),
            RoleSchema(role="decode", weight=2, max_batch_tokens=2048),
        ],
    )
    rows = declared_roles(block)
    assert [(r.role, r.weight, r.max_batch_tokens) for r in rows] == [
        ("prefill", 1, 32768),
        ("decode", 2, 2048),
    ]
    assert rows[0].engine_args == {"max-num-seqs": "4"}
    # Detached: the caller decides which workload they belong to.
    assert all(getattr(r, "workload_id", None) is None for r in rows)


def test_the_topology_survives_the_read_and_write_round_trip():
    """A stored workload read back as the request that would have created it, roles included.

    The update path applies a patch to exactly this object and validates the result whole, so a
    topology that did not survive the round trip would be silently dropped by any patch that did
    not restate it.
    """
    artifact = an_artifact()
    workload = a_workload(serving_topology="disaggregated", device_count=8)
    roles = [a_role("prefill", weight=1), a_role("decode", weight=2, max_batch_tokens=2048)]

    request = serving_request(workload, artifact, roles)
    assert request.topology.serving_topology == "disaggregated"
    assert [(r.role, r.weight) for r in request.topology.roles] == [("prefill", 1), ("decode", 2)]
    assert declared_roles(request.topology)[1].max_batch_tokens == 2048

    view = serving_view(workload, artifact, roles).topology
    assert view.serving_topology == "disaggregated"
    assert view.group_devices == 24


def test_an_aggregated_workload_reads_back_as_aggregated_with_no_roles():
    artifact = an_artifact()
    view = serving_view(a_workload(), artifact, ()).topology
    assert view.serving_topology == "aggregated"
    assert view.roles == []
    assert view.group_devices == a_workload().device_count


# --------------------------------------------------------------------------- router


USE1_ROUTER = RouterSchema(mode="kv", queue_threshold=1.0, queue_policy="wspt", kv_overlap_score_credit=8.0)
"""The four settings the Kimi K3 benchmark on site-d passes with, hand-patched there before this block existed."""


def test_the_router_the_use1_benchmark_runs_on_is_accepted():
    check_router(USE1_ROUTER)
    check_router(RouterSchema())


def test_kv_settings_beside_an_undeclared_mode_are_accepted_because_the_default_is_kv():
    check_router(RouterSchema(queue_threshold=0.0, kv_overlap_score_credit=0.0))


@pytest.mark.parametrize(
    "router",
    [
        RouterSchema(mode="direct"),
        RouterSchema(mode="kv-aware"),
        RouterSchema(queue_threshold=1.0, queue_policy="lcfs"),
        RouterSchema(queue_threshold=-0.1),
        RouterSchema(queue_threshold=float("nan")),
        RouterSchema(kv_overlap_score_credit=-1.0),
        RouterSchema(kv_overlap_score_credit=float("inf")),
        RouterSchema(queue_policy="wspt"),
        RouterSchema(mode="round-robin", kv_overlap_score_credit=8.0),
        RouterSchema(mode="random", queue_threshold=1.0),
    ],
    ids=[
        "direct-mode",
        "unknown-mode",
        "lcfs-policy",
        "negative-threshold",
        "nan-threshold",
        "negative-credit",
        "infinite-credit",
        "policy-without-threshold",
        "credit-beside-round-robin",
        "threshold-beside-random",
    ],
)
def test_a_router_dynamo_would_refuse_or_never_read_is_refused(router):
    """Each of these is a frontend that exits at startup, or a setting that silently does nothing."""
    with pytest.raises(Invalid):
        check_router(router)


def test_a_router_is_validated_with_the_rest_of_the_configuration():
    with pytest.raises(Invalid):
        check_serving_request(a_request(router=RouterSchema(mode="direct")), an_artifact())


@pytest.mark.parametrize("router", [None, {}, {"mode": None, "queue_threshold": None}])
def test_an_undeclared_router_leaves_the_digest_byte_identical(router):
    """Every stored digest predates the block, and a digest that moved would re-render its deployment.

    Asserted against the hash spelled out without a router key, not merely against another call, so
    a router key that crept into the payload as `{}` or as nulls fails here.
    """
    artifact = Artifact(uid=new_uid(ARTIFACT))
    workload = a_workload(router=router)
    payload = {"artifact": str(artifact.uid)}
    for key, attribute in DIGEST_FIELDS:
        payload[key] = _canonical(getattr(workload, attribute))
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert serving_digest(workload, artifact, ()) == "svg_" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_a_declared_router_moves_the_digest_and_spelling_does_not():
    artifact = Artifact(uid=new_uid(ARTIFACT))
    base = serving_digest(a_workload(), artifact, ())
    declared = serving_digest(a_workload(router={"queue_threshold": 1.0}), artifact, ())

    assert declared != base
    assert serving_digest(a_workload(router={"queue_threshold": 1}), artifact, ()) == declared
    assert serving_digest(a_workload(router={"mode": None, "queue_threshold": 1.0}), artifact, ()) == declared
    assert serving_digest(a_workload(router={"queue_threshold": 1.0, "mode": "kv"}), artifact, ()) != declared


def test_only_declared_router_settings_are_stored():
    artifact = an_artifact()
    declared = a_workload()
    apply_serving(declared, a_request(router=RouterSchema(queue_threshold=1.0, queue_policy="wspt")), artifact, ())
    assert declared.router == {"queue_threshold": 1.0, "queue_policy": "wspt"}

    undeclared = a_workload()
    apply_serving(undeclared, a_request(), artifact, ())
    assert undeclared.router == {}
    stored = undeclared.serving_digest
    undeclared.router = None
    assert serving_digest(undeclared, artifact, ()) == stored


def test_the_router_reads_back_as_declared_with_nulls_for_the_rest():
    workload = a_workload(router={"queue_threshold": 1.0})
    assert rendered(workload).router == RouterSchema(queue_threshold=1.0)
    assert rendered(a_workload()).router == RouterSchema()
    assert serving_request(workload, an_artifact()).router == RouterSchema(queue_threshold=1.0)
    assert router_block({"mode": None, "kv_overlap_score_credit": 8}) == RouterSchema(kv_overlap_score_credit=8.0)


def test_the_wire_carries_a_router_only_when_one_is_declared():
    """None, not an empty block, so a workload that declares nothing sends the frame it always sent."""
    artifact = an_artifact()
    assert _serving(a_workload(), artifact).router is None
    assert _serving(a_workload(router={}), artifact).router is None

    workload = a_workload(router=declared_router(msgspec.to_builtins(USE1_ROUTER)))
    spec = _serving(workload, artifact)
    assert spec.router == RouterSpec(mode="kv", queue_threshold=1.0, queue_policy="wspt", kv_overlap_score_credit=8.0)
    assert json.loads(msgspec.json.encode(spec))["router"] == {
        "mode": "kv",
        "queue_threshold": 1.0,
        "queue_policy": "wspt",
        "kv_overlap_score_credit": 8.0,
    }
    assert json.loads(msgspec.json.encode(_serving(workload, artifact).router)) == json.loads(
        msgspec.json.encode(RouterSpec(**declared_router(workload.router)))
    )
