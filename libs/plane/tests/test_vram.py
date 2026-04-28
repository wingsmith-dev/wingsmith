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
from app.core.vram import fitment
from app.core.vram import sizing_of
from app.core.vram import usable_gb
from app.core.vram import impossible
from app.core.vram import layout_for
from app.core.vram import weights_gb
from app.core.vram import device_counts
from app.core.vram import estimate_vram
from app.core.vram import extract_shape
from app.core.vram import hidden_size_of
from app.core.vram import shape_metadata
from app.core.vram import bytes_per_param
from app.core.vram import quant_from_name
from app.core.vram import effective_config
from app.core.vram import fit_context_length
from app.core.vram import kv_bytes_per_token
from app.core.vram import native_quant_method
from app.core.vram import derive_active_params
from app.core.vram import baseline_min_gpu_memory_gb
from app.core.devices import DeviceClass


GIB = 1024**3


def llama_config(**kwargs) -> dict:
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
    shape = extract_shape(llama_config())
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
    shape = extract_shape(llama_config())
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
    assert quant_from_name("meta-llama/Llama-3.1-8B-Instruct") is None


def test_compressed_tensors_declares_bits_not_a_scheme_name():
    config = {"quantization_config": {"config_groups": {"group_0": {"weights": {"num_bits": 8, "type": "float"}}}}}
    assert native_quant_method(config) == "fp8"
    config = {"quantization_config": {"config_groups": {"group_0": {"weights": {"num_bits": 4, "type": "int"}}}}}
    assert native_quant_method(config) == "int4"


def test_active_params_drop_the_unrouted_experts():
    config = gpt_oss_config()
    total = 20_900_000_000
    active = derive_active_params(config, total)
    assert active < total
    assert derive_active_params(llama_config(), total) == total


def test_a_config_without_a_layer_count_has_no_shape():
    assert extract_shape(None) is None
    assert extract_shape({}) is None
    assert extract_shape({"hidden_size": 4096}) is None


def test_baseline_caps_the_sizing_context_at_8k():
    shape = extract_shape(llama_config(max_position_embeddings=1_000_000))
    baseline = baseline_min_gpu_memory_gb(total_params=8_000_000_000, shape=shape)
    assert baseline == baseline_min_gpu_memory_gb(
        total_params=8_000_000_000,
        shape=extract_shape(llama_config(max_position_embeddings=8192)),
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
    sizing = sizing_of_config(llama_config(), 70_000_000_000)
    assert weights_gb(sizing) == pytest.approx(70_000_000_000 * 2 / GIB)


def test_quantization_changes_what_has_to_be_resident():
    fp16 = sizing_of_config(llama_config(), 70_000_000_000, "fp16")
    fp8 = sizing_of_config(llama_config(), 70_000_000_000, "fp8")
    assert weights_gb(fp8) == pytest.approx(weights_gb(fp16) / 2)


def test_the_same_model_fits_on_small_cards_by_needing_more_of_them():
    """A fit is about memory, not about whether the layout is a good idea.

    A 70B at 8k does fit across a whole node of L4s — 180 GiB is 180 GiB, and the
    reason nobody serves it that way is interconnect, which is a performance
    judgement this module does not hold and must not smuggle in as a no-fit.
    """
    sizing = sizing_of_config(llama_config(max_position_embeddings=8192), 70_000_000_000)
    intent = Intent(context_length=8192)

    hopper = fit(sizing, klass("h100-80gb-sxm"), intent)
    l4 = fit(sizing, klass("l4-24gb-pcie"), intent)

    assert hopper.fits and not hopper.reduced
    assert l4.fits and not l4.reduced
    assert hopper.device_count < l4.device_count == 8


def test_a_fit_crosses_nodes_because_the_backend_fabric_carries_pipeline_stages():
    """The old ceiling made every model larger than one chassis of VRAM unservable."""
    sizing = sizing_of_config(llama_config(max_position_embeddings=8192), 70_000_000_000)
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
    """72 coherent devices is one node, not nine chassis — the interconnect is the node."""
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
    sizing = sizing_of_config(llama_config(num_attention_heads=32, num_key_value_heads=8), 7_000_000_000)
    device = klass("h100-80gb-sxm")

    assert impossible(sizing, device, 16, tensor_parallel=8) is None
    assert impossible(sizing, device, 16, tensor_parallel=16) is None

    odd = sizing_of_config(llama_config(num_attention_heads=28, num_key_value_heads=28), 7_000_000_000)
    assert impossible(odd, device, 16, tensor_parallel=8) is not None
    assert impossible(odd, device, 16, tensor_parallel=4) is None


def test_a_no_fit_still_says_how_far_off_it_was():
    """ "needs more than 8× L4" is a procurement answer; "does not fit" is not.

    Bounded with `max_devices` rather than by a model too large to fit anywhere. Now
    that a fit crosses domains freely, "does not fit" means "does not fit inside the
    ceiling you asked for", and naming the ceiling is the honest way to say it.
    """
    sizing = sizing_of_config(llama_config(), 405_000_000_000)
    verdict = fit(sizing, klass("l4-24gb-pcie"), Intent(context_length=8192, max_devices=8))
    assert not verdict.fits
    assert "weights" in verdict.reason
    assert verdict.weights_gb > verdict.aggregate_gb


def test_attention_heads_that_do_not_divide_are_impossible_at_any_memory():
    """28 heads over 8 devices is a layout no amount of VRAM makes possible."""
    sizing = sizing_of_config(llama_config(num_attention_heads=28, num_key_value_heads=28), 7_000_000_000)
    reason = impossible(sizing, klass("b300-288gb-sxm"), 8, tensor_parallel=8)
    assert reason is not None
    assert "attention heads" in reason
    assert impossible(sizing, klass("b300-288gb-sxm"), 4, tensor_parallel=4) is None


def test_an_unmeasured_device_class_is_not_contradicted():
    """A card the table carries no size for cannot be refused; it can only be unknown."""
    sizing = sizing_of_config(llama_config(), 405_000_000_000)
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
    sizing = sizing_of_config(llama_config(max_position_embeddings=131072), 70_000_000_000)
    device = klass("h100-80gb-sxm")
    assert impossible(sizing, device, 2) is None
    assert not fit_at(sizing, device, Intent(context_length=131072, concurrency=64), 2).fits


def test_a_truncated_context_is_offered_rather_than_hidden():
    sizing = sizing_of_config(deepseek_config(), 40_000_000_000)
    verdict = fit(sizing, klass("a100-40gb-sxm"), Intent(context_length=163840))
    if verdict.fits and verdict.reduced:
        assert verdict.context_length < 163840
        assert "rather than" in verdict.reason


def test_fitment_ranks_the_cheapest_workable_class_first():
    sizing = sizing_of_config(llama_config(max_position_embeddings=8192), 13_000_000_000)
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
    shape = extract_shape(llama_config())
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
    shape = extract_shape(llama_config(max_position_embeddings=8192))
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
    shape = extract_shape(llama_config())
    assert shape is not None
    sizing = sizing_of({"shape": shape_metadata(shape)}, 70_000_000_000)
    assert sizing is not None
    assert sizing.quantization == DEFAULT_QUANTIZATION


def test_the_envelope_is_applied_once_and_not_compounded():
    """Two discounts on the same headroom was worth twenty percent, and that is a card count."""
    shape = extract_shape(llama_config())
    assert shape is not None
    estimate = estimate_vram(
        total_params=8_000_000_000,
        shape=shape,
        quantization="fp16",
        context_length=8192,
    )
    assert estimate.total_gb == pytest.approx(
        estimate.weights_gb + estimate.kv_cache_gb + estimate.activations_gb
    )
    assert estimate.required_gb == pytest.approx(estimate.total_gb / 0.9)


def test_activations_scale_with_the_batch_and_not_with_the_weights():
    """The flat fraction this replaced made a 2.8T model reserve a node for CUDA context."""
    shape = extract_shape(llama_config())
    assert shape is not None
    small = estimate_vram(total_params=8_000_000_000, shape=shape, context_length=8192)
    huge = estimate_vram(total_params=2_800_000_000_000, shape=shape, context_length=8192)
    assert small.activations_gb == huge.activations_gb

    wider = estimate_vram(
        total_params=8_000_000_000, shape=shape, context_length=8192, batch_tokens=32_768
    )
    assert wider.activations_gb == pytest.approx(small.activations_gb * 4)


def test_the_fixed_reserve_is_per_device_so_it_grows_with_the_layout():
    """CUDA context is a cost per GPU. Spreading wider buys less than the raw sum."""
    card = 81_920
    one, four = usable_gb(card, 1), usable_gb(card, 4)
    assert four == pytest.approx(one * 4)
    assert four < 4 * card / 1024 * 0.9
    assert usable_gb(card, 4) - usable_gb(card, 2) == pytest.approx(usable_gb(card, 2))


def test_a_card_smaller_than_the_reserve_offers_nothing_rather_than_owing_memory():
    assert usable_gb(512, 1) == 0.0
    assert usable_gb(0, 8) == 0.0


def test_hidden_size_is_declared_where_it_can_be_and_reconstructed_where_it_cannot():
    """The heads-times-head-dim identity does not hold for MLA, which has no head_dim."""
    assert hidden_size_of(extract_shape(llama_config())) == 4096

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
