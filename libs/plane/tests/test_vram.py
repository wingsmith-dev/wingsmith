import itertools

import pytest

from app.core import devices
from app.core.vram import MAX_DEVICES
from app.core.vram import BYTES_PER_PARAM
from app.core.vram import DEFAULT_QUANTIZATION
from app.core.vram import DEFAULT_DEVICES_PER_NODE
from app.core.vram import Intent
from app.core.vram import Layout
from app.core.vram import Sizing
from app.core.vram import ModelShape
from app.core.vram import fit
from app.core.vram import fit_at
from app.core.vram import is_moe
from app.core.vram import ladder
from app.core.vram import fitment
from app.core.vram import sizing_of
from app.core.vram import usable_gb
from app.core.vram import impossible
from app.core.vram import layout_for
from app.core.vram import weights_gb
from app.core.vram import weight_terms
from app.core.vram import device_counts
from app.core.vram import estimate_vram
from app.core.vram import expert_params
from app.core.vram import extract_shape
from app.core.vram import hidden_size_of
from app.core.vram import kv_cache_bytes
from app.core.vram import shape_metadata
from app.core.vram import bytes_per_param
from app.core.vram import quant_from_name
from app.core.vram import effective_config
from app.core.vram import fit_context_length
from app.core.vram import kv_bytes_per_token
from app.core.vram import expert_quant_method
from app.core.vram import native_quant_method
from app.core.vram import derive_active_params
from app.core.vram import baseline_min_gpu_memory_gb
from app.core.devices import DeviceClass


GIB = 1024**3


def dense_config(**kwargs) -> dict:
    base = {
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "hidden_size": 4096,
        "max_position_embeddings": 8192,
    }
    return base | kwargs


def gpt_oss_config(**kwargs) -> dict:
    base = {
        "num_hidden_layers": 24,
        "num_attention_heads": 64,
        "num_key_value_heads": 8,
        "hidden_size": 2880,
        "head_dim": 64,
        "intermediate_size": 2880,
        "num_local_experts": 32,
        "num_experts_per_tok": 4,
        "max_position_embeddings": 131072,
        "quantization_config": {"quant_method": "mxfp4"},
    }
    return base | kwargs


def gemma_config(**kwargs) -> dict:
    """Interleaved attention the older way: five local layers to every global one."""
    base = {
        "num_hidden_layers": 62,
        "num_attention_heads": 32,
        "num_key_value_heads": 16,
        "head_dim": 128,
        "hidden_size": 5376,
        "max_position_embeddings": 131072,
        "sliding_window": 1024,
        "sliding_window_pattern": 6,
    }
    return base | kwargs


def deepseek_config(**kwargs) -> dict:
    base = {
        "num_hidden_layers": 61,
        "num_attention_heads": 128,
        "num_key_value_heads": 128,
        "hidden_size": 7168,
        "kv_lora_rank": 512,
        "qk_rope_head_dim": 64,
        "max_position_embeddings": 163840,
    }
    return base | kwargs


def test_a_dense_fp16_model_sizes_at_two_bytes_per_param():
    shape = extract_shape(dense_config())
    estimate = estimate_vram(
        total_params=8_000_000_000,
        shape=shape,
        quantization="fp16",
        context_length=8192,
    )
    assert estimate.weights_gb == pytest.approx(8_000_000_000 * 2 / GIB)
    assert estimate.total_gb > estimate.weights_gb
    assert estimate.required_gb > estimate.total_gb


def test_a_natively_quantized_moe_is_not_sized_at_fp16():
    """gpt-oss-20b ships mxfp4; sizing it at 2 bytes/param overcounts 4x."""
    config = gpt_oss_config()
    assert is_moe(config)
    quant = native_quant_method(config, "openai/gpt-oss-20b")
    assert quant == "mxfp4"
    assert bytes_per_param(quant) == 0.5

    shape = extract_shape(config)
    estimate = estimate_vram(
        total_params=20_900_000_000,
        shape=shape,
        quantization=quant,
        context_length=8192,
    )
    fp16 = estimate_vram(
        total_params=20_900_000_000,
        shape=shape,
        quantization="fp16",
        context_length=8192,
    )
    assert estimate.weights_gb == pytest.approx(20_900_000_000 * 0.5 / GIB)
    assert fp16.weights_gb == pytest.approx(estimate.weights_gb * 4)
    assert estimate.weights_gb < 11


def test_an_mla_config_sizes_kv_from_the_compressed_latent():
    shape = extract_shape(deepseek_config())
    assert shape.kv_lora_rank == 512
    assert shape.num_key_value_heads is None

    assert kv_bytes_per_token(shape) == 61 * (512 + 64) * 2

    naive = 2 * 61 * 128 * (7168 // 128) * 2
    assert kv_bytes_per_token(shape) < naive


def test_a_sliding_layer_holds_its_window_and_not_the_sequence():
    """The sizing that refused a workload which would have run.

    gemma-4-26b declares 131k and runs 51 of its 62 layers on a 1024-token window. Sized
    as if all 62 were global it needs 122 GiB, which is two RTX PRO 6000s and then a
    per-device floor of 122 GiB on a card that has 96, so the plane refused the single
    card the model actually serves on. The local layers are two orders of magnitude
    cheaper than the arithmetic charged them.
    """
    shape = extract_shape(gemma_config())
    assert (shape.sliding_window, shape.sliding_layers) == (1024, 51)

    naive = kv_bytes_per_token(shape) * 131_072
    assert kv_cache_bytes(shape, 131_072) < naive / 5

    estimate = estimate_vram(total_params=26_000_000_000, shape=shape, context_length=131_072)
    assert estimate.kv_cache_gb < 15
    assert estimate.required_gb < 96


def test_layer_types_names_every_layer_and_outranks_the_pattern():
    """What current Transformers writes, and it is the explicit answer where both appear."""
    types = ["sliding_attention"] * 20 + ["full_attention"] * 4
    shape = extract_shape(gpt_oss_config(sliding_window=128, layer_types=types))

    assert (shape.sliding_window, shape.sliding_layers) == (128, 20)


def test_a_window_with_no_pattern_is_a_model_where_every_layer_is_local():
    """Mistral's original sliding window: no interleaving, the whole stack is local."""
    shape = extract_shape(dense_config(sliding_window=4096, max_position_embeddings=32_768))

    assert shape.sliding_layers == shape.num_hidden_layers
    assert kv_cache_bytes(shape, 32_768) == pytest.approx(kv_cache_bytes(shape, 4096))


def test_sliding_window_parameters_a_repo_has_turned_off_are_not_read():
    """Qwen3 ships the fields and does not use them, and reading them anyway halves a real cost."""
    shape = extract_shape(dense_config(sliding_window=4096, use_sliding_window=False))

    assert (shape.sliding_window, shape.sliding_layers) == (None, 0)
    assert kv_cache_bytes(shape, 8192) == pytest.approx(kv_bytes_per_token(shape) * 8192)


def test_full_attention_still_costs_the_whole_sequence_on_every_layer():
    """The ordinary case, unchanged: no window declared means nothing is discounted."""
    shape = extract_shape(dense_config())

    assert kv_cache_bytes(shape, 8192) == pytest.approx(kv_bytes_per_token(shape) * 8192)


def test_text_config_nesting_is_flattened():
    config = {
        "model_type": "qwen3_5",
        "quantization_config": {"quant_method": "fp8"},
        "text_config": {
            "num_hidden_layers": 48,
            "hidden_size": 5120,
            "num_attention_heads": 40,
            "num_key_value_heads": 8,
        },
    }
    eff = effective_config(config)
    assert eff["num_hidden_layers"] == 48
    assert eff["quantization_config"]["quant_method"] == "fp8"

    shape = extract_shape(config)
    assert shape.num_hidden_layers == 48
    assert shape.head_dim == 128
    assert native_quant_method(config) == "fp8"


def test_fit_context_length_reduces_the_window_when_the_full_one_does_not_fit():
    shape = extract_shape(gpt_oss_config())
    fit = fit_context_length(
        total_params=20_900_000_000,
        shape=shape,
        quantization="mxfp4",
        declared_context_length=131072,
        concurrency=64,
        available_gb=40.0,
    )
    assert fit.fits
    assert fit.reduced
    assert fit.context_length < 131072
    assert fit.required_gb <= 40.0


def test_fit_context_length_keeps_the_declared_window_when_it_fits():
    shape = extract_shape(dense_config())
    fit = fit_context_length(
        total_params=8_000_000_000,
        shape=shape,
        quantization="fp16",
        declared_context_length=8192,
        concurrency=1,
        available_gb=80.0,
    )
    assert fit.context_length == 8192
    assert not fit.reduced
    assert fit.fits


def test_fit_context_length_reports_the_floor_as_not_fitting():
    shape = extract_shape(deepseek_config())
    fit = fit_context_length(
        total_params=671_000_000_000,
        shape=shape,
        quantization="fp8",
        declared_context_length=163840,
        concurrency=1,
        available_gb=80.0,
    )
    assert fit.context_length == 4096
    assert not fit.fits


def test_quant_from_name_reads_the_repo_suffix():
    assert quant_from_name("zai-org/GLM-4.6-FP8") == "fp8"
    assert quant_from_name("some/Model-AWQ") == "awq"
    assert quant_from_name("Qwen/Qwen3.5-9B") is None


def test_compressed_tensors_declares_bits_not_a_scheme_name():
    config = {"quantization_config": {"config_groups": {"group_0": {"weights": {"num_bits": 8, "type": "float"}}}}}
    assert native_quant_method(config) == "fp8"
    config = {"quantization_config": {"config_groups": {"group_0": {"weights": {"num_bits": 4, "type": "int"}}}}}
    assert native_quant_method(config) == "int4"


def test_multimodal_quantization_is_read_from_the_nested_text_config():
    """A multimodal repo nests `quantization_config` under `text_config`.

    Reading only the top level returned None, which `sizing_of` turns into the
    fp16 default: a fourfold overcount on 4-bit weights, and four times the
    cards.
    """
    config = {
        "model_type": "kimi_k3",
        "vision_config": {"hidden_size": 1152},
        "text_config": {
            "num_hidden_layers": 93,
            "quantization_config": {
                "quant_method": "compressed-tensors",
                "format": "mxfp4-pack-quantized",
                "config_groups": {"group_0": {"weights": {"num_bits": 4, "type": "float"}}},
            },
        },
    }
    assert native_quant_method(config) == "mxfp4"


def test_compressed_tensors_format_beats_its_generic_method_name():
    """`quant_method` is the literal 'compressed-tensors' for every bucket it holds.

    The bit width alone says fp4, which sizes correctly but is not a string any
    engine accepts as `--quantization`. `format` carries the real scheme.
    """
    quant = {
        "quant_method": "compressed-tensors",
        "format": "mxfp4-pack-quantized",
        "config_groups": {"group_0": {"weights": {"num_bits": 4, "type": "float"}}},
    }
    assert native_quant_method({"quantization_config": quant}) == "mxfp4"
    del quant["format"]
    assert native_quant_method({"quantization_config": quant}) == "fp4"


def test_a_2_8t_mxfp4_model_fits_one_node_of_b300():
    """The regression that started this: 2.8T MXFP4 sized at fp16 asked for four nodes."""
    shape = ModelShape(
        num_hidden_layers=93,
        num_key_value_heads=None,
        head_dim=None,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
        context_length=1048576,
        num_attention_heads=96,
        hidden_size=7168,
    )
    device = next(d for d in devices.table().classes if d.name == "b300-288gb-sxm")
    intent = Intent(context_length=1048576, devices_per_node=8)
    sizing = Sizing(total_params=2_800_000_000_000, shape=shape, quantization="mxfp4")
    fitted = fit(sizing, device, intent)
    assert fitted.fits
    assert fitted.device_count == 8
    assert fitted.node_count == 1

    overcounted = fit(Sizing(total_params=2_800_000_000_000, shape=shape, quantization="fp16"), device, intent)
    assert overcounted.device_count == 32


def test_active_params_drop_the_unrouted_experts():
    config = gpt_oss_config()
    total = 20_900_000_000
    active = derive_active_params(config, total)
    assert active < total
    assert derive_active_params(dense_config(), total) == total


def test_a_config_without_a_layer_count_has_no_shape():
    assert extract_shape(None) is None
    assert extract_shape({}) is None
    assert extract_shape({"hidden_size": 4096}) is None


def test_baseline_caps_the_sizing_context_at_8k():
    shape = extract_shape(dense_config(max_position_embeddings=1_000_000))
    baseline = baseline_min_gpu_memory_gb(total_params=8_000_000_000, shape=shape)
    assert baseline == baseline_min_gpu_memory_gb(
        total_params=8_000_000_000,
        shape=extract_shape(dense_config(max_position_embeddings=8192)),
    )


def test_unknown_quantization_falls_back_to_fp16():
    assert bytes_per_param("wat") == BYTES_PER_PARAM["fp16"]


def sizing_of_config(config: dict, total_params: int, quantization: str = "fp16") -> Sizing:
    shape = extract_shape(config)
    assert shape is not None
    return Sizing(total_params=total_params, shape=shape, quantization=quantization)


def klass(name: str) -> DeviceClass:
    entry = devices.table().get(name)
    assert entry is not None, f"{name} is missing from the contract device table"
    return entry


def test_weight_bytes_are_arithmetic_not_an_estimate():
    """The one number in this module with no fudge factor, so the one worth refusing over."""
    sizing = sizing_of_config(dense_config(), 70_000_000_000)
    assert weights_gb(sizing) == pytest.approx(70_000_000_000 * 2 / GIB)


def test_quantization_changes_what_has_to_be_resident():
    fp16 = sizing_of_config(dense_config(), 70_000_000_000, "fp16")
    fp8 = sizing_of_config(dense_config(), 70_000_000_000, "fp8")
    assert weights_gb(fp8) == pytest.approx(weights_gb(fp16) / 2)


def test_the_same_model_fits_on_small_cards_by_needing_more_of_them():
    """A fit is about memory, not about whether the layout is a good idea.

    A 70B at 8k does fit across a whole node of L4s: 180 GiB is 180 GiB, and the
    reason nobody serves it that way is interconnect, which is a performance
    judgement this module does not hold and must not smuggle in as a no-fit.
    """
    sizing = sizing_of_config(dense_config(max_position_embeddings=8192), 70_000_000_000)
    intent = Intent(context_length=8192)

    hopper = fit(sizing, klass("h100-80gb-sxm"), intent)
    l4 = fit(sizing, klass("l4-24gb-pcie"), intent)

    assert hopper.fits and not hopper.reduced
    assert l4.fits and not l4.reduced
    assert hopper.device_count < l4.device_count == 8


def test_fit_crosses_nodes_over_the_backend_fabric():
    """The old ceiling made every model larger than one chassis of VRAM unservable."""
    sizing = sizing_of_config(dense_config(max_position_embeddings=8192), 70_000_000_000)
    wide = fit(sizing, klass("l4-24gb-pcie"), Intent(context_length=8192, devices_per_node=8))

    assert wide.fits
    assert wide.device_count == 8
    assert wide.node_count == 1

    narrow = fit(sizing, klass("l4-24gb-pcie"), Intent(context_length=8192, max_devices=4))
    assert not narrow.fits
    assert narrow.device_count == 4


def test_a_frontier_model_is_answered_in_domains_rather_than_refused():
    """Kimi-K3-sized weights: the answer is a rack order, not a red row."""
    sizing = Sizing(total_params=2_800_000_000_000, shape=None, quantization="fp8")
    intent = Intent(context_length=8192, devices_per_node=8)

    b300 = fit(sizing, klass("b300-288gb-sxm"), intent)
    assert b300.fits
    assert b300.device_count == 16
    assert b300.node_count == 2
    assert (b300.tensor_parallel, b300.pipeline_parallel) == (8, 2)
    assert "2 nodes of 8×" in b300.reason

    b200 = fit(sizing, klass("b200-180gb-sxm"), intent)
    assert b200.fits
    assert b200.device_count == 32
    assert b200.node_count == 4
    assert (b200.tensor_parallel, b200.pipeline_parallel) == (8, 4)


def test_one_nvl72_rack_is_a_single_node_and_says_so():
    """72 coherent devices is one node, not nine chassis: the interconnect is the node."""
    sizing = Sizing(total_params=2_800_000_000_000, shape=None, quantization="fp8")

    chassis = fit(sizing, klass("b300-288gb-sxm"), Intent(context_length=8192, devices_per_node=8))
    rack = fit(sizing, klass("b300-288gb-sxm"), Intent(context_length=8192, devices_per_node=72))

    assert chassis.device_count == rack.device_count == 16
    assert chassis.node_count == 2
    assert rack.node_count == 1
    assert rack.pipeline_parallel == 1
    assert rack.tensor_parallel == 16


def test_a_layout_keeps_tensor_parallelism_inside_one_node():
    """The one split that genuinely does not work is a tensor group over the fabric."""
    assert layout_for(16, 8) == Layout(device_count=16, node_count=2, tensor_parallel=8, pipeline_parallel=2)
    assert layout_for(16, 72) == Layout(device_count=16, node_count=1, tensor_parallel=16, pipeline_parallel=1)
    assert layout_for(1, 8) == Layout(device_count=1, node_count=1, tensor_parallel=1, pipeline_parallel=1)
    assert layout_for(128, 8).tensor_parallel == 8
    assert layout_for(128, 8).node_count == 16


def test_a_node_size_that_is_not_a_power_of_two_is_capped_not_divided():
    """NVL72 is 72 coherent devices; the tensor degree is still a power of two."""
    laid = layout_for(72, 72)
    assert laid.node_count == 1
    assert laid.tensor_parallel == 8
    assert laid.pipeline_parallel == 9


def test_the_device_ladder_is_powers_of_two_to_a_runaway_bound():
    counts = device_counts()
    assert counts[:5] == [1, 2, 4, 8, 16]
    assert counts[-1] == MAX_DEVICES
    assert all(b == a * 2 for a, b in itertools.pairwise(counts))
    assert DEFAULT_DEVICES_PER_NODE in counts


def test_heads_are_checked_against_the_tensor_degree_not_the_device_count():
    """28 heads over 16 devices as tensor-8 by pipeline-2 divides by 8, and 8 is fine."""
    sizing = sizing_of_config(dense_config(num_attention_heads=32, num_key_value_heads=8), 7_000_000_000)
    device = klass("h100-80gb-sxm")

    assert impossible(sizing, device, 16, tensor_parallel=8) is None
    assert impossible(sizing, device, 16, tensor_parallel=16) is None

    odd = sizing_of_config(dense_config(num_attention_heads=28, num_key_value_heads=28), 7_000_000_000)
    assert impossible(odd, device, 16, tensor_parallel=8) is not None
    assert impossible(odd, device, 16, tensor_parallel=4) is None


def test_a_no_fit_still_says_how_far_off_it_was():
    """ "needs more than 8× L4" is a procurement answer; "does not fit" is not.

    Bounded with `max_devices`, not by a model too large to fit anywhere. Now
    that a fit crosses domains freely, "does not fit" means "does not fit inside the
    ceiling you asked for", and naming the ceiling is the accurate way to say it.
    """
    sizing = sizing_of_config(dense_config(), 405_000_000_000)
    verdict = fit(sizing, klass("l4-24gb-pcie"), Intent(context_length=8192, max_devices=8))
    assert not verdict.fits
    assert "weights" in verdict.reason
    assert verdict.weights_gb > verdict.aggregate_gb


def test_attention_heads_that_do_not_divide_are_impossible_at_any_memory():
    """28 heads over 8 devices is a layout no amount of VRAM makes possible."""
    sizing = sizing_of_config(dense_config(num_attention_heads=28, num_key_value_heads=28), 7_000_000_000)
    reason = impossible(sizing, klass("b300-288gb-sxm"), 8, tensor_parallel=8)
    assert reason is not None
    assert "attention heads" in reason
    assert impossible(sizing, klass("b300-288gb-sxm"), 4, tensor_parallel=4) is None


def test_an_unmeasured_device_class_is_not_contradicted():
    """A card the table carries no size for cannot be refused; it can only be unknown."""
    sizing = sizing_of_config(dense_config(), 405_000_000_000)
    unknown = DeviceClass(
        name="mystery-card",
        label="Mystery Card",
        architecture="",
        vendor="nvidia",
        memory_mib=0,
        form_factor="pcie",
        aliases={},
    )
    assert impossible(sizing, unknown, 1) is None
    assert not fit_at(sizing, unknown, Intent(context_length=8192), 1).fits


def test_impossible_is_silent_about_everything_an_estimate_covers():
    """A shape that holds the weights but no context is advice, never a refusal."""
    sizing = sizing_of_config(dense_config(max_position_embeddings=131072), 70_000_000_000)
    device = klass("h100-80gb-sxm")
    assert impossible(sizing, device, 2) is None
    assert not fit_at(sizing, device, Intent(context_length=131072, concurrency=64), 2).fits


def test_a_truncated_context_is_offered_rather_than_hidden():
    sizing = sizing_of_config(deepseek_config(), 40_000_000_000)
    verdict = fit(sizing, klass("a100-40gb-sxm"), Intent(context_length=163840))
    if verdict.fits and verdict.reduced:
        assert verdict.context_length < 163840
        assert "instead of" in verdict.reason


def test_the_ladder_offers_every_count_that_serves_and_not_only_the_smallest():
    """The recommendation is one answer; how much of a node to spend is the question."""
    sizing = sizing_of_config(dense_config(max_position_embeddings=131072), 70_000_000_000)
    device = klass("h200-141gb-sxm")
    intent = Intent(context_length=131072, devices_per_node=8)

    options = ladder(sizing, device, intent)
    counts = [option.device_count for option in options]

    assert counts == sorted(counts)
    assert fit(sizing, device, intent).device_count in counts
    assert len(counts) > 1
    assert all(option.fits for option in options)


def test_the_ladder_stops_at_a_node_when_a_replica_fits_inside_one():
    """Widening past the fabric to buy KV cache is a call nothing here can price."""
    sizing = sizing_of_config(dense_config(max_position_embeddings=8192), 13_000_000_000)
    device = klass("h200-141gb-sxm")

    options = ladder(sizing, device, Intent(context_length=8192, devices_per_node=8))
    assert max(option.device_count for option in options) == 8


def test_the_ladder_reaches_the_recommendation_when_it_spans_several_nodes():
    sizing = sizing_of_config(dense_config(max_position_embeddings=8192), 400_000_000_000)
    device = klass("h100-80gb-sxm")
    intent = Intent(context_length=8192, devices_per_node=8)

    chosen = fit(sizing, device, intent)
    assert chosen.node_count > 1
    assert max(option.device_count for option in ladder(sizing, device, intent)) == chosen.device_count


def test_the_ladder_keeps_a_trimmed_window_as_a_choice_rather_than_dropping_it():
    """Fewer cards at a shorter window is a trade somebody chooses to make."""
    sizing = sizing_of_config(dense_config(max_position_embeddings=131072), 70_000_000_000)
    device = klass("h200-141gb-sxm")
    intent = Intent(context_length=131072, concurrency=32, devices_per_node=8)

    options = ladder(sizing, device, intent)
    chosen = fit(sizing, device, intent)
    smaller = [option for option in options if option.device_count < chosen.device_count]

    assert smaller, "a count below the recommendation should still be offered"
    assert all(option.reduced for option in smaller)
    assert all(option.context_length < intent.context_length for option in smaller)


def test_a_class_that_fits_nowhere_offers_no_ladder():
    sizing = sizing_of_config(dense_config(), 2_800_000_000_000)
    device = klass("l4-24gb-pcie")
    intent = Intent(context_length=8192, max_devices=8)

    assert not fit(sizing, device, intent).fits
    assert ladder(sizing, device, intent) == []


def test_fitment_ranks_the_cheapest_workable_class_first():
    sizing = sizing_of_config(dense_config(max_position_embeddings=8192), 13_000_000_000)
    results = fitment(sizing, devices.table().classes, Intent(context_length=8192))

    assert len(results) == len(devices.table().classes)
    assert results[0].fits
    fitting = [r for r in results if r.fits]
    assert [r.fits for r in results] == sorted((r.fits for r in results), reverse=True)
    assert fitting[0].device_count <= fitting[-1].device_count


def test_a_stored_artifact_can_be_sized_without_the_network():
    """The shape is persisted at resolve time precisely so a fit needs no HTTP."""
    shape = extract_shape(gpt_oss_config())
    assert shape is not None
    metadata = {"quantization": "mxfp4", "shape": shape_metadata(shape)}

    sizing = sizing_of(metadata, 120_000_000_000)
    assert sizing is not None
    assert sizing.quantization == "mxfp4"
    assert sizing.shape == shape


def test_only_a_missing_parameter_count_makes_an_artifact_unsizable():
    """A missing shape costs the KV cache, not the whole answer."""
    shape = extract_shape(dense_config())
    assert shape is not None
    full = {"quantization": "fp16", "shape": shape_metadata(shape)}

    assert sizing_of(full, None) is None
    assert sizing_of({}, None) is None

    shapeless = sizing_of({"quantization": "fp16"}, 70_000_000_000)
    assert shapeless is not None
    assert shapeless.shape is None
    assert sizing_of({"shape": {}}, 70_000_000_000).shape is None

    sized = sizing_of(full, 70_000_000_000)
    assert sized is not None
    assert sized.shape == shape


def test_a_shapeless_artifact_still_says_which_cards_are_too_small():
    """The weights bound needs no config, and it is the half with no fudge in it."""
    sizing = Sizing(total_params=70_000_000_000, shape=None, quantization="fp16")
    intent = Intent(context_length=8192)

    huge = Sizing(total_params=405_000_000_000, shape=None, quantization="fp16")
    small = fit(huge, klass("l4-24gb-pcie"), Intent(context_length=8192, max_devices=8))
    assert not small.fits
    assert not small.kv_counted
    assert "weights" in small.reason

    big = fit(sizing, klass("h100-80gb-sxm"), intent)
    assert big.fits
    assert not big.kv_counted
    assert big.device_count == 2
    assert big.required_gb == big.weights_gb
    assert big.context_length == 0
    assert "no context window is costed" in big.reason


def test_a_shapeless_fit_is_a_floor_and_never_larger_than_a_real_one():
    """The floor has to sit under the estimate, or it is not a floor."""
    shape = extract_shape(dense_config(max_position_embeddings=8192))
    assert shape is not None
    sized = Sizing(total_params=70_000_000_000, shape=shape, quantization="fp16")
    shapeless = Sizing(total_params=70_000_000_000, shape=None, quantization="fp16")
    intent = Intent(context_length=8192)

    device = klass("h100-80gb-sxm")
    assert fit_at(shapeless, device, intent, 4).required_gb <= fit_at(sized, device, intent, 4).required_gb
    assert fit(shapeless, device, intent).device_count <= fit(sized, device, intent).device_count


def test_impossible_still_refuses_a_shapeless_artifact_that_cannot_be_held():
    """Sharding is unchecked without a head count; the weights are not."""
    sizing = Sizing(total_params=405_000_000_000, shape=None, quantization="fp16")
    assert impossible(sizing, klass("l4-24gb-pcie"), 1) is not None
    assert impossible(sizing, klass("b300-288gb-sxm"), 8) is None


def test_sizing_defaults_to_fp16_when_the_artifact_names_no_quantization():
    shape = extract_shape(dense_config())
    assert shape is not None
    sizing = sizing_of({"shape": shape_metadata(shape)}, 70_000_000_000)
    assert sizing is not None
    assert sizing.quantization == DEFAULT_QUANTIZATION


def test_the_envelope_is_applied_once_and_not_compounded():
    """Two discounts on the same headroom was worth twenty percent, and that is a card count."""
    shape = extract_shape(dense_config())
    assert shape is not None
    estimate = estimate_vram(
        total_params=8_000_000_000,
        shape=shape,
        quantization="fp16",
        context_length=8192,
    )
    assert estimate.total_gb == pytest.approx(estimate.weights_gb + estimate.kv_cache_gb + estimate.activations_gb)
    assert estimate.required_gb == pytest.approx(estimate.total_gb / 0.9)


def test_activations_scale_with_the_batch_and_not_with_the_weights():
    """The flat fraction this replaced made a 2.8T model reserve a node for CUDA context."""
    shape = extract_shape(dense_config())
    assert shape is not None
    small = estimate_vram(total_params=8_000_000_000, shape=shape, context_length=8192)
    huge = estimate_vram(total_params=2_800_000_000_000, shape=shape, context_length=8192)
    assert small.activations_gb == huge.activations_gb

    wider = estimate_vram(total_params=8_000_000_000, shape=shape, context_length=8192, batch_tokens=32_768)
    assert wider.activations_gb == pytest.approx(small.activations_gb * 4)


def test_the_fixed_reserve_is_per_device_so_it_grows_with_the_layout():
    """CUDA context is a cost per GPU. Spreading wider buys less than the raw sum."""
    card = 81_920
    one, four = usable_gb(card, 1), usable_gb(card, 4)
    assert four == pytest.approx(one * 4)
    assert four < 4 * card / 1024 * 0.9
    assert usable_gb(card, 4) - usable_gb(card, 2) == pytest.approx(usable_gb(card, 2))


def test_card_smaller_than_reserve_offers_nothing():
    assert usable_gb(512, 1) == 0.0
    assert usable_gb(0, 8) == 0.0


def test_hidden_size_is_declared_or_reconstructed():
    """The heads-times-head-dim identity does not hold for MLA, which has no head_dim."""
    assert hidden_size_of(extract_shape(dense_config())) == 4096

    reconstructed = ModelShape(
        num_hidden_layers=32,
        num_key_value_heads=8,
        head_dim=256,
        kv_lora_rank=None,
        qk_rope_head_dim=None,
        context_length=8192,
        num_attention_heads=24,
    )
    assert hidden_size_of(reconstructed) == 24 * 256

    mla = extract_shape(deepseek_config())
    assert mla is not None
    assert mla.head_dim is None
    assert hidden_size_of(mla) == 7168


def test_a_twenty_seven_billion_model_at_131k_fits_on_two_rtx_6000():
    """The case the compounded overhead was refusing: 84.1 GB needed of 84.4 offered."""
    shape = ModelShape(
        num_hidden_layers=64,
        num_key_value_heads=4,
        head_dim=256,
        kv_lora_rank=None,
        qk_rope_head_dim=None,
        context_length=262_144,
        num_attention_heads=24,
        hidden_size=6144,
    )
    sizing = Sizing(total_params=27_781_427_952, shape=shape, quantization="fp16")
    device = klass("rtx-6000-ada-48gb-pcie")

    at_131k = fit(sizing, device, Intent(context_length=131_072))
    assert at_131k.fits and not at_131k.reduced
    assert at_131k.device_count == 2

    at_256k = fit(sizing, device, Intent(context_length=262_144))
    assert at_256k.fits and not at_256k.reduced
    assert at_256k.device_count == 4, "256k genuinely does not fit: weights plus cache exceed the cards"


def deepseek_v4_pro_config(**kwargs) -> dict:
    """`deepseek-ai/DeepSeek-V4-Pro-0813` as the Hub publishes it, trimmed to the sizing fields.

    Three things in one config that each broke a different term, which is why it is
    here whole, not as three synthetic shapes. It writes its MLA latent as
    `head_dim` with a single KV head and no `kv_lora_rank`; it declares a
    `sliding_window` beside an `index_topk` that is sparse selection and not a
    smaller cache; and it is quantized twice, fp8 in the header and fp4 for the
    experts that are 94% of it.
    """
    base = {
        "architectures": ["DeepseekV4ForCausalLM"],
        "expert_dtype": "fp4",
        "head_dim": 512,
        "hidden_size": 7168,
        "index_head_dim": 128,
        "index_n_heads": 64,
        "index_topk": 1024,
        "max_position_embeddings": 1048576,
        "model_type": "deepseek_v4",
        "moe_intermediate_size": 3072,
        "n_routed_experts": 384,
        "n_shared_experts": 1,
        "num_attention_heads": 128,
        "num_experts_per_tok": 6,
        "num_hidden_layers": 61,
        "num_key_value_heads": 1,
        "q_lora_rank": 1536,
        "qk_rope_head_dim": 64,
        "quantization_config": {"quant_method": "fp8", "fmt": "e4m3", "activation_scheme": "dynamic"},
        "sliding_window": 128,
        "torch_dtype": "bfloat16",
        "vocab_size": 129280,
    }
    return base | kwargs


DEEPSEEK_V4_PRO_PARAMS = 1_650_497_936_906
"""`safetensors.total` for the 0813 checkpoint, which is what the plane stores."""


def deepseek_v4_pro_sizing() -> Sizing:
    config = deepseek_v4_pro_config()
    return Sizing(
        total_params=DEEPSEEK_V4_PRO_PARAMS,
        shape=extract_shape(config),
        quantization=native_quant_method(config, "deepseek-ai/DeepSeek-V4-Pro-0813"),
        expert_params=expert_params(config),
        expert_quantization=expert_quant_method(config),
    )


def test_deepseek_v4_pro_at_800k_fits_one_node_of_h200():
    """vLLM publishes a one-node H200 recipe at 800k; the plane offered two nodes and no way to argue.

    The pin is the node count and the ladder together. A fit that is right while the
    ladder still hides the eight-device option leaves the console offering only the
    sixteen, which is the shape the operator actually hit.
    """
    sizing = deepseek_v4_pro_sizing()
    device = klass("h200-141gb-sxm")
    intent = Intent(context_length=800_000, devices_per_node=8)

    fitted = fit(sizing, device, intent)
    assert fitted.fits and not fitted.reduced
    assert fitted.device_count == 8
    assert fitted.node_count == 1
    assert fitted.context_length == 800_000
    assert fitted.required_gb < usable_gb(device.memory_mib, 8)

    assert [option.device_count for option in ladder(sizing, device, intent)] == [8]


def test_a_latent_kv_cache_is_found_under_head_dim_when_kv_lora_rank_is_absent():
    """Transformers 4.57 writes MLA as one KV head holding the whole latent.

    Read as ordinary attention it charges key and value separately across a 512-wide
    head: 1.78x the 61 x (512 + 64) x 2 bytes a token actually costs.
    """
    shape = extract_shape(deepseek_v4_pro_config())

    assert shape.kv_lora_rank == 512
    assert shape.qk_rope_head_dim == 64
    assert shape.num_key_value_heads is None
    assert kv_bytes_per_token(shape) == 61 * (512 + 64) * 2

    standard = 2 * 61 * 1 * 512 * 2
    assert kv_bytes_per_token(shape) < standard


def test_a_sliding_window_beside_a_sparse_index_is_not_a_smaller_cache():
    """`sliding_window: 128` with no `layer_types` used to charge all 61 layers 128 tokens.

    0.015 GiB for an 800k window, three orders of magnitude under. Detecting the
    latent first is what stops the standard branch ever seeing the field.
    """
    shape = extract_shape(deepseek_v4_pro_config())

    assert shape.sliding_window is None
    assert shape.sliding_layers == 0
    assert kv_cache_bytes(shape, 800_000) == kv_bytes_per_token(shape) * 800_000


def test_a_mixed_precision_checkpoint_is_charged_at_both_of_its_precisions():
    """`expert_dtype` against `quantization_config`, and the experts are the checkpoint.

    Charging all 1.65 T at the headline fp8 is 1,537 GiB for weights that are about
    960 GB on disk. That 1.6x is exactly one extra H200 node.
    """
    sizing = deepseek_v4_pro_sizing()
    terms = weight_terms(sizing)

    assert [term.quantization for term in terms] == ["fp8", "fp4"]
    assert sum(term.params for term in terms) == DEEPSEEK_V4_PRO_PARAMS
    assert terms[1].params / DEEPSEEK_V4_PRO_PARAMS > 0.9

    uniform = Sizing(total_params=DEEPSEEK_V4_PRO_PARAMS, shape=sizing.shape, quantization="fp8")
    assert weights_gb(sizing) < weights_gb(uniform) * 0.6


def test_a_refusal_names_every_precision_it_charged():
    """The operator's real complaint: a wrong number nobody could argue with.

    "the weights need 817 GB" is unarguable. Naming the terms lets somebody who
    knows the checkpoint see which one is wrong.
    """
    reason = impossible(deepseek_v4_pro_sizing(), klass("h200-141gb-sxm"), 4)

    assert reason is not None
    assert "dense weights at fp8" in reason
    assert "routed experts at fp4" in reason


def test_an_artifact_with_no_expert_precision_sizes_exactly_as_it_always_did():
    """The pair is additive. Every single-precision checkpoint is untouched by it."""
    shape = extract_shape(dense_config())
    plain = Sizing(total_params=70_000_000_000, shape=shape, quantization="fp8")

    assert weights_gb(plain) == 70_000_000_000 * BYTES_PER_PARAM["fp8"] / GIB
    assert len(weight_terms(plain)) == 1
    assert weight_terms(plain)[0].label == "weights"


def test_the_expert_precision_survives_being_written_down_and_read_back():
    """A fit is computed from the stored artifact, never from `config.json` again.

    So the second precision has to be persisted beside the shape. Losing it on the way
    to the database would put DeepSeek-V4-Pro back on two nodes the moment anybody
    fitted it from the catalogue, not from a resolve.
    """
    config = deepseek_v4_pro_config()
    metadata = {
        "quantization": native_quant_method(config, "deepseek-ai/DeepSeek-V4-Pro-0813"),
        "expert_parameter_count": expert_params(config),
        "expert_quantization": expert_quant_method(config),
        "shape": shape_metadata(extract_shape(config)),
    }
    stored = sizing_of(metadata, DEEPSEEK_V4_PRO_PARAMS)

    assert stored == deepseek_v4_pro_sizing()

    fitted = fit(stored, klass("h200-141gb-sxm"), Intent(context_length=800_000, devices_per_node=8))
    assert fitted.device_count == 8 and fitted.node_count == 1


def test_an_artifact_stored_before_the_expert_fields_existed_still_reads_back():
    """Every artifact already in the catalogue has neither key, and must size as before."""
    stored = sizing_of({"quantization": "fp8", "shape": shape_metadata(extract_shape(dense_config()))}, 70_000_000_000)

    assert stored.expert_params is None
    assert stored.expert_quantization is None
    assert weights_gb(stored) == 70_000_000_000 * BYTES_PER_PARAM["fp8"] / GIB
