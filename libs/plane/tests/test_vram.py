import pytest

from app.modules.recipes.vram import BYTES_PER_PARAM
from app.modules.recipes.vram import is_moe
from app.modules.recipes.vram import estimate_vram
from app.modules.recipes.vram import extract_shape
from app.modules.recipes.vram import bytes_per_param
from app.modules.recipes.vram import quant_from_name
from app.modules.recipes.vram import effective_config
from app.modules.recipes.vram import fit_context_length
from app.modules.recipes.vram import kv_bytes_per_token
from app.modules.recipes.vram import native_quant_method
from app.modules.recipes.vram import derive_active_params
from app.modules.recipes.vram import baseline_min_gpu_memory_gb


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
