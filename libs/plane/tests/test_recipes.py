import inspect
import datetime as dt

from types import SimpleNamespace

import pytest
import msgspec

from app.config import settings
from app.core.ids import RECIPE
from app.core.ids import ARTIFACT
from app.core.ids import new_uid
from app.core.vram import extract_shape
from app.core.vram import shape_metadata
from app.db.models import Recipe
from app.db.notify import MemoryNotifier
from app.hub.messages import RecipeDesired
from app.core.exceptions import Invalid
from app.modules.recipes.routes import RecipeRoutes
from app.modules.recipes.schemas import RecipeSchema
from app.modules.recipes.schemas import RecipeCacheSchema
from app.modules.recipes.schemas import RecipeShapeSchema
from app.modules.recipes.schemas import RecipeUsageSchema
from app.modules.recipes.schemas import RecipeCreateRequest
from app.modules.recipes.schemas import RecipeOverlaySchema
from app.modules.recipes.schemas import RecipeServingSchema
from app.modules.recipes.schemas import RecipeUpdateRequest
from app.modules.recipes.schemas import RecipeReasoningSchema
from app.modules.recipes.schemas import RecipeCapabilitiesSchema
from app.modules.recipes.schemas import RecipeBenchmarkSummarySchema
from app.modules.recipes.services import RecipeService
from app.modules.recipes.services import _revision
from app.modules.recipes.services import sizing_for
from app.modules.recipes.services import check_shape
from app.modules.recipes.services import check_quantization
from app.modules.recipes.services import serving_args
from app.modules.recipes.services import check_serving
from app.modules.recipes.services import check_capacity
from app.modules.recipes.services import check_footprint
from app.modules.recipes.services import resolve_engine_image
from app.modules.recipes.services import resolved_engine_args
from app.modules.recipes.repository import RecipeRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def repo():
    return RecipeRepository(db=None)


def sql(query) -> str:
    return str(query)


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


def test_a_shape_without_a_device_class_is_not_a_shape():
    """Tensor-parallel-8 means nothing spread across an A100 and an H100."""
    with pytest.raises(Invalid):
        check_shape(**shape(device_class=""))
    with pytest.raises(Invalid):
        check_shape(**shape(device_class="   "))


def test_device_class_is_required_at_the_request_boundary_too():
    fields = RecipeCreateRequest.__struct_fields__
    assert "device_class" in fields
    with pytest.raises(TypeError):
        RecipeCreateRequest(artifact="a", name="n", engine="vllm", engine_version="0.6", context_length=8192)


def test_device_class_cannot_be_cleared_by_a_patch():
    assert RecipeUpdateRequest.__annotations__["device_class"] == str | msgspec.UnsetType
    assert type(None) not in (RecipeUpdateRequest.__annotations__["device_class"]).__args__


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


def test_context_length_lives_on_the_recipe_not_on_a_catalog_entry():
    assert "context_length" in RecipeSchema.__struct_fields__
    assert "context_length" in RecipeCreateRequest.__struct_fields__


def test_kv_cache_is_recipe_configuration_and_not_the_weight_cache():
    fields = set(RecipeCacheSchema.__struct_fields__)
    assert fields == {"kv_cache_dtype", "kv_cache_fraction", "prefix_cache_enabled", "prefix_cache_config"}
    assert not [f for f in RecipeSchema.__struct_fields__ if "warm" in f or "resident" in f]


def test_the_shape_is_returned_as_one_thing_class_included():
    assert "device_class" in RecipeShapeSchema.__struct_fields__
    assert "device_class" not in RecipeSchema.__struct_fields__


def test_the_overlay_is_a_passthrough_on_every_edge_of_the_surface():
    """What the plane does not model still has to be sayable, readable, patchable."""
    assert set(RecipeOverlaySchema.__struct_fields__) == {"pod_spec", "dynamo"}
    assert "overlay" in RecipeSchema.__struct_fields__
    assert RecipeCreateRequest.__struct_fields__.index("overlay") >= 0
    assert "overlay" in RecipeUpdateRequest.__struct_fields__


def test_an_absent_overlay_defaults_to_an_empty_one():
    data = RecipeCreateRequest(
        artifact="art_1",
        name="llama-vllm",
        engine="vllm",
        engine_version="0.6.0",
        device_class="h100",
        context_length=8192,
    )
    assert data.overlay == {}
    assert RecipeUpdateRequest().to_dict(exclude_unset=True) == {}


def test_the_weights_a_recipe_serves_cannot_be_repointed_by_a_patch():
    fields = RecipeUpdateRequest.__struct_fields__
    assert "artifact" not in fields
    assert "version" not in fields


def test_update_request_leaves_absent_fields_unset():
    data = RecipeUpdateRequest(engine_version="0.7.0")
    assert data.to_dict(exclude_unset=True) == {"engine_version": "0.7.0"}
    assert RecipeUpdateRequest(quantization=None).to_dict(exclude_unset=True) == {"quantization": None}


def test_ids_are_prefixed_and_lowercase():
    uid = new_uid(RECIPE)
    assert uid.startswith("rec_") and uid == uid.lower()


def test_label_selector_becomes_a_predicate_per_key(repo):
    """One `->>` comparison per key, so two labels narrow rather than widen."""
    text = sql(repo.list_query(limit=10, labels={"tier": "gold", "team": "search"}))
    assert text.count("demand_recipe.labels ->>") == 2
    assert "->>" not in sql(repo.list_query(limit=10))


def test_revision_is_derived_from_the_last_write_not_stored():
    assert _revision(Recipe(created_at=EPOCH, updated_at=None)) == str(int(EPOCH.timestamp() * 1_000_000))


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=41,
            artifact="llama-70b",
            engine="vllm",
            device_class="h100",
            search="fp8",
        )
    )
    assert "FROM demand_recipe JOIN demand_artifact" in text
    assert "demand_recipe.seq > " in text
    assert "demand_artifact.name = " in text
    assert "demand_recipe.engine = " in text
    assert "demand_recipe.device_class = " in text
    assert "lower(demand_recipe.name) LIKE lower(" in text
    assert "ORDER BY demand_recipe.seq ASC" in text


def test_artifact_filter_resolves_an_id_against_uid_not_name(repo):
    assert "demand_artifact.uid = " in sql(repo.list_query(limit=10, artifact=new_uid(ARTIFACT)))


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_get_query_resolves_a_name_or_an_id(repo):
    assert "demand_recipe.name = " in sql(repo.get_query("llama-70b-fp8"))
    assert "demand_recipe.uid = " in sql(repo.get_query(new_uid(RECIPE)))


def test_a_bare_name_names_a_family_and_resolves_to_its_newest_version(repo):
    assert "ORDER BY demand_recipe.version DESC" in sql(repo.get_query("llama-70b-fp8"))


def test_controller_exposes_full_crud():
    handlers = {h.handler_name for h in RecipeRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_recipes",
        "create_recipe",
        "get_recipe",
        "update_recipe",
        "delete_recipe",
        "recipe_usage",
        "recipe_benchmarks_summary",
    }


def test_workload_usage_counts_the_workloads_that_name_the_recipe(repo):
    text = sql(repo.workload_usage_query(Recipe(uid=new_uid(RECIPE))))
    assert "count(demand_workload.id)" in text
    assert "FROM demand_workload" in text
    assert "demand_workload.recipe_id" in text


def test_benchmark_rollup_reads_only_graded_non_discarded_runs(repo):
    text = sql(repo.benchmark_runs_query(Recipe(uid=new_uid(RECIPE))))
    assert "ops_benchmark_run.recipe_id" in text
    assert "ops_benchmark_run.discard = false" in text
    assert "ops_benchmark_run.summary" in text
    assert "ops_benchmark_run.harness" in text


class _Row:
    def __init__(self, harness, harness_version, summary):
        self.harness = harness
        self.harness_version = harness_version
        self.summary = summary


class _GetRow(tuple):
    """Stands in for the SQLAlchemy Row the recipe repository returns: indexable
    at [0] for the recipe and carrying the ``artifact_uid`` label as an attribute."""

    @property
    def artifact_uid(self):
        return self[1]


class _StubRepo:
    def __init__(self, recipe, rows):
        self._recipe = recipe
        self._rows = rows

    async def get(self, ref):
        return _GetRow((self._recipe, new_uid(ARTIFACT)))

    async def benchmark_runs(self, recipe):
        return self._rows


async def test_benchmark_summary_takes_the_peak_and_skips_unreadable_runs():
    """Only runs whose harness-and-version adapter resolves a throughput contribute."""
    recipe = a_recipe()
    rows = [
        _Row("aiperf", "0.4.0", {"output_token_throughput_per_user": {"avg": 120.0}}),
        _Row("aiperf", "0.4.0", {"output_token_throughput_per_user": {"avg": 260.0}}),
        _Row("aiperf", None, {"output_token_throughput_per_user": {"avg": 999.0}}),
        _Row("aiperf", "0.4.0", {"nothing_here": True}),
    ]
    svc = RecipeService(db=None, notifier=MemoryNotifier())
    svc.repo = _StubRepo(recipe, rows)
    summary = await svc.benchmarks_summary(recipe.uid)
    assert summary.run_count == 4
    assert summary.graded_run_count == 2
    assert summary.peak_output_tokens_per_second == 260.0


async def test_benchmark_summary_peak_is_none_when_nothing_can_be_read():
    recipe = a_recipe()
    svc = RecipeService(db=None, notifier=MemoryNotifier())
    svc.repo = _StubRepo(recipe, [_Row("unknown-harness", "1.0.0", {"x": 1})])
    summary = await svc.benchmarks_summary(recipe.uid)
    assert summary.graded_run_count == 0
    assert summary.peak_output_tokens_per_second is None


def test_usage_schema_is_a_count_not_the_workloads_themselves():
    assert set(RecipeUsageSchema.__struct_fields__) == {"recipe_id", "workload_count"}


def test_benchmark_summary_carries_a_peak_that_can_be_absent():
    assert set(RecipeBenchmarkSummarySchema.__struct_fields__) == {
        "recipe_id",
        "run_count",
        "graded_run_count",
        "peak_output_tokens_per_second",
    }
    annotation = RecipeBenchmarkSummarySchema.__annotations__["peak_output_tokens_per_second"]
    assert type(None) in annotation.__args__


def test_class_filter_is_exposed_as_class_not_device_class():
    parameter = inspect.signature(RecipeRoutes.list_recipes.fn).parameters["device_class"]
    assert parameter.default.query == "class"


def test_conditional_update_reads_if_match_from_the_header():
    parameter = inspect.signature(RecipeRoutes.update_recipe.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_delete_refuses_while_a_deployment_still_names_the_recipe():
    from app.modules.recipes.services import RecipeService

    assert "Refuses" in (RecipeService.delete.__doc__ or "")


def serving(**kwargs) -> RecipeServingSchema:
    reasoning = kwargs.pop("reasoning", None)
    block = RecipeServingSchema(**kwargs)
    if reasoning is not None:
        block.reasoning = RecipeReasoningSchema(**reasoning)
    return block


def capabilities(**kwargs) -> RecipeCapabilitiesSchema:
    return RecipeCapabilitiesSchema(**kwargs)


def a_recipe(**kwargs) -> Recipe:
    """A Recipe row as SQLAlchemy hands one back, with no server defaults applied."""
    base = {
        "uid": new_uid(RECIPE),
        "name": "llama-vllm",
        "version": 1,
        "engine": "vllm",
        "engine_version": "0.6.0",
        "engine_image": None,
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
        "cpu_cores": None,
        "memory_mib": None,
        "disk_gib": None,
        "labels": {},
        "overlay": {},
        "serving": {},
        "capabilities": {},
        "created_at": EPOCH,
        "updated_at": None,
    }
    return Recipe(**(base | kwargs))


def rendered(recipe: Recipe) -> RecipeSchema:
    return RecipeService(db=None, notifier=MemoryNotifier()).schema(recipe, new_uid(ARTIFACT))


def test_a_recipe_with_no_serving_block_still_round_trips():
    """Every recipe written before the block existed reads back as the defaults."""
    for stored in ({}, None):
        schema = rendered(a_recipe(serving=stored, capabilities=stored))
        assert schema.serving == RecipeServingSchema()
        assert schema.capabilities == RecipeCapabilitiesSchema()
        assert schema.serving.reasoning.parser is None
        assert schema.serving.context_overflow == "reject"
        assert schema.capabilities.input_modalities == ["text"]
        decoded = msgspec.json.decode(msgspec.json.encode(schema), type=RecipeSchema)
        assert decoded.serving == schema.serving
        assert decoded.capabilities == schema.capabilities


def test_an_absent_serving_block_is_the_defaults_at_the_request_boundary_too():
    data = RecipeCreateRequest(
        artifact="art_1",
        name="llama-vllm",
        engine="vllm",
        engine_version="0.6.0",
        device_class="h100",
        context_length=8192,
    )
    assert data.serving == RecipeServingSchema()
    assert data.capabilities == RecipeCapabilitiesSchema()
    assert check_serving(data.serving, data.capabilities, prefix_cache_enabled=False) is None
    assert RecipeUpdateRequest().to_dict(exclude_unset=True) == {}


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
    )
    claimed = capabilities(
        reasoning_supported=True,
        structured_output_modes=["text", "json_object", "json_schema"],
        tool_calling_supported=True,
        tool_choice_modes=["auto", "required"],
        parallel_tool_calls=True,
        input_modalities=["text", "image"],
        prompt_cache_mode="automatic",
        prompt_cache_ttl_seconds=300,
        asserted_by="operator",
        asserted_at=EPOCH,
        source_uri="https://example.invalid/model-card",
    )
    assert check_serving(declared, claimed, prefix_cache_enabled=True) is None

    schema = rendered(a_recipe(serving=msgspec.to_builtins(declared), capabilities=msgspec.to_builtins(claimed)))
    assert schema.serving == declared
    assert schema.capabilities == claimed

    decoded = msgspec.json.decode(msgspec.json.encode(schema), type=RecipeSchema)
    assert decoded.serving == declared
    assert decoded.capabilities == claimed
    assert decoded.capabilities.asserted_at == EPOCH


def test_the_serving_block_is_typed_rather_than_another_free_dict():
    assert RecipeSchema.__annotations__["serving"] is RecipeServingSchema
    assert RecipeSchema.__annotations__["capabilities"] is RecipeCapabilitiesSchema
    assert "reasoning" in RecipeServingSchema.__struct_fields__
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"thinking": True}, RecipeServingSchema)


def test_a_reasoning_parser_named_with_thinking_off_is_refused():
    """The contradiction: how it reasons, declared on a recipe claiming it cannot."""
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


def test_a_tool_parser_on_a_recipe_that_does_not_claim_tool_calling_is_refused():
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
        {"input_modalities": ["image"]},
        {"input_modalities": ["text", "smell"]},
        {"prompt_cache_mode": "sometimes"},
        {"prompt_cache_ttl_seconds": -1},
    ],
)
def test_an_unknown_or_repeated_member_of_a_claimed_set_is_refused(block):
    with pytest.raises(Invalid):
        check_serving(serving(), capabilities(**block), prefix_cache_enabled=True)


def test_an_image_limit_needs_image_among_the_modalities():
    with pytest.raises(Invalid):
        check_serving(serving(max_images_per_request=4), capabilities(), prefix_cache_enabled=False)
    with pytest.raises(Invalid):
        check_serving(
            serving(max_images_per_request=0),
            capabilities(input_modalities=["text", "image"]),
            prefix_cache_enabled=False,
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


def test_a_typed_serving_field_beats_the_same_flag_in_engine_args():
    """The escape hatch keeps the flags nothing models, and loses the ones it does."""
    recipe = a_recipe(
        engine_args={"--reasoning-parser": "deepseek_r1", "--tool-call-parser": "llama3_json", "max-num-seqs": "64"},
        serving=msgspec.to_builtins(
            serving(reasoning={"parser": "qwen3"}, tool_call_parser="hermes", structured_output_backend="xgrammar")
        ),
    )
    schema = rendered(recipe)
    assert schema.resolved_engine_args["reasoning-parser"] == "qwen3"
    assert schema.resolved_engine_args["tool-call-parser"] == "hermes"
    assert schema.resolved_engine_args["guided-decoding-backend"] == "xgrammar"
    assert schema.resolved_engine_args["max-num-seqs"] == "64"
    assert schema.engine_args == recipe.engine_args


def test_engine_args_keeps_every_flag_the_serving_block_does_not_model():
    recipe = a_recipe(engine_args={"--enforce-eager": "", "swap-space": "8"})
    schema = rendered(recipe)
    assert schema.engine_args == {"--enforce-eager": "", "swap-space": "8"}
    assert schema.resolved_engine_args == {"enforce-eager": "", "swap-space": "8"}


def test_the_flag_a_typed_field_speaks_for_is_the_engines_own_spelling():
    block = serving(structured_output_backend="xgrammar")
    assert serving_args("vllm", block) == {"guided-decoding-backend": "xgrammar"}
    assert serving_args("sglang", block) == {"grammar-backend": "xgrammar"}
    assert serving_args("trtllm", block) == {}
    assert serving_args("vllm", serving(max_images_per_request=4)) == {"limit-mm-per-prompt": "image=4"}


def test_a_default_serving_block_speaks_for_no_flag_at_all():
    """Which is what keeps this additive on every recipe that already exists."""
    assert serving_args("vllm", RecipeServingSchema()) == {}
    assert resolved_engine_args("vllm", {"--dtype": "bfloat16"}, RecipeServingSchema()) == {"dtype": "bfloat16"}


def test_a_capability_claim_never_reaches_a_cluster():
    """A cluster has no use for a claim about itself, exactly as with `imported_args`."""
    assert "capabilities" not in RecipeDesired.__struct_fields__
    assert "imported_args" not in RecipeDesired.__struct_fields__


def test_the_serving_block_is_declared_on_the_recipe_and_patchable():
    assert "serving" in RecipeCreateRequest.__struct_fields__
    assert "serving" in RecipeUpdateRequest.__struct_fields__
    assert "capabilities" in RecipeCreateRequest.__struct_fields__
    assert "capabilities" in RecipeUpdateRequest.__struct_fields__
    assert RecipeUpdateRequest(serving=serving()).to_dict(exclude_unset=True) == {"serving": serving()}


def artifact_row(*, parameter_count: int | None, quantization: str = "fp16", with_shape: bool = True):
    shape = extract_shape(
        {
            "num_hidden_layers": 32,
            "num_attention_heads": 32,
            "num_key_value_heads": 8,
            "hidden_size": 4096,
            "max_position_embeddings": 8192,
        }
    )
    assert shape is not None
    metadata = {"quantization": quantization}
    if with_shape:
        metadata["shape"] = shape_metadata(shape)
    return SimpleNamespace(parameter_count=parameter_count, metadata_=metadata)


def test_a_shape_that_cannot_hold_the_weights_is_refused():
    """The arithmetic half of a fit: 130 GB of weights do not go on one 80 GB card."""
    sizing = sizing_for(artifact_row(parameter_count=70_000_000_000), "fp16")
    with pytest.raises(Invalid):
        check_capacity(sizing, "h100-80gb-sxm", 1, 1)
    assert check_capacity(sizing, "h100-80gb-sxm", 4, 4) is None


def test_the_recipes_quantization_decides_what_has_to_be_resident():
    """Serving fp16 weights at fp8 halves the footprint, and the recipe owns that call."""
    artifact = artifact_row(parameter_count=70_000_000_000, quantization="fp16")
    with pytest.raises(Invalid):
        check_capacity(sizing_for(artifact, "fp16"), "h100-80gb-sxm", 1, 1)
    assert check_capacity(sizing_for(artifact, "int4"), "h100-80gb-sxm", 1, 1) is None


def test_a_capacity_check_is_silent_about_what_it_cannot_measure():
    """An unsizeable artifact or an unknown class is not grounds to refuse a recipe."""
    assert check_capacity(None, "h100-80gb-sxm", 1, 1) is None
    assert check_capacity(sizing_for(artifact_row(parameter_count=None), "fp16"), "h100-80gb-sxm", 1, 1) is None
    assert (
        check_capacity(sizing_for(artifact_row(parameter_count=70_000_000_000, with_shape=False), "fp16"), "h100", 1, 1)
        is None
    )
    sizing = sizing_for(artifact_row(parameter_count=405_000_000_000), "fp16")
    assert check_capacity(sizing, "some-card-the-table-has-never-heard-of", 1, 1) is None


def test_a_context_window_that_does_not_fit_is_not_a_refusal():
    """The estimate has a 15% fudge in it, so enforcing it would refuse working recipes."""
    sizing = sizing_for(artifact_row(parameter_count=70_000_000_000), "fp16")
    assert check_capacity(sizing, "h100-80gb-sxm", 2, 2) is None


def test_engine_image_is_resolved_from_the_engine_and_version():
    """The common case: an author picks an engine, not a registry address."""
    assert resolve_engine_image("vllm", "1.4.1", None) == "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1"


def test_a_declared_engine_image_always_wins():
    """Pinning an exact image is the reason the field is writable."""
    assert resolve_engine_image("vllm", "1.4.1", "my.registry/vllm:dev") == "my.registry/vllm:dev"


def test_latest_resolves_to_a_real_tag():
    """`latest` is not a tag these registries publish; taking it verbatim is a pull failure."""
    resolved = resolve_engine_image("vllm", "latest", None)
    assert resolved is not None
    assert resolved.endswith(f":{settings.ENGINE_IMAGE_TAG_DEFAULT}")


def test_an_empty_version_resolves_to_a_real_tag():
    resolved = resolve_engine_image("vllm", "", None)
    assert resolved is not None
    assert resolved.endswith(f":{settings.ENGINE_IMAGE_TAG_DEFAULT}")


def test_an_unknown_engine_has_no_default():
    """Inventing an address would trade a visible gap for an ImagePullBackOff."""
    assert resolve_engine_image("some-engine-nobody-configured", "1.0", None) is None


def test_the_engine_name_is_matched_case_insensitively():
    assert resolve_engine_image("VLLM", "1.4.1", None) is not None


def test_a_malformed_repo_pair_does_not_break_the_write_path():
    """One bad character in an env var must not be why a recipe cannot be created."""
    original = settings.ENGINE_IMAGE_REPOS
    settings.ENGINE_IMAGE_REPOS = "garbage,,vllm=repo/vllm"
    try:
        assert resolve_engine_image("vllm", "1.4.1", None) == "repo/vllm:1.4.1"
    finally:
        settings.ENGINE_IMAGE_REPOS = original
