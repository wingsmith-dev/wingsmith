"""VRAM sizing for a recipe's device shape.

Pure, dependency-free estimation of how much GPU memory a model needs to serve,
broken into the four buckets that actually drive the number: weights, KV cache,
a flat runtime overhead, and the framework's pre-allocation headroom.

A recipe declares a device shape — class, count, parallelism, context length —
and this module is what decides whether that shape is physically possible for
the artifact it points at, and what context window it can actually carry.

This module is also the single source of truth for interpreting a HuggingFace
``config.json``, which varies sharply across model families: multimodal models
nest the language config under ``text_config``; DeepSeek/GLM/Kimi use MLA
(latent attention) where the KV cache is a compressed ``kv_lora_rank`` vector
rather than ``num_key_value_heads x head_dim``; and many modern repos ship
natively quantized (``quantization_config``), so fp16 weight sizing overcounts
by up to 4x.
"""

from __future__ import annotations

import math

from dataclasses import dataclass


BYTES_PER_PARAM: dict[str, float] = {
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8": 1.0,
    "int8": 1.0,
    "awq": 0.5,
    "gptq": 0.5,
    "int4": 0.5,
    "mxfp4": 0.5,
    "nvfp4": 0.5,
    "fp4": 0.5,
}

_KV_BYTES = 2.0
"""KV cache is held in fp16/bf16 (2 bytes) regardless of weight quantization."""

_OVERHEAD_FRACTION = 0.15
"""Flat fudge for activations, CUDA graphs, and allocator fragmentation."""

_DEFAULT_GPU_MEMORY_UTILIZATION = 0.9
"""vLLM's default ``--gpu-memory-utilization``: only ~90% of a card is usable."""

_GIB = 1024**3

DEFAULT_QUANTIZATION = "fp16"


@dataclass
class VramEstimate:
    """Per-bucket VRAM breakdown for a single serving configuration, in GiB."""

    weights_gb: float
    kv_cache_gb: float
    overhead_gb: float
    total_gb: float
    required_gb: float


@dataclass
class ModelShape:
    """Attention shape needed for KV-cache sizing, pulled from ``config.json``.

    For standard (GQA/MHA) attention, KV is sized from ``num_key_value_heads ×
    head_dim``. For MLA models, ``kv_lora_rank`` is set and the KV cache is the
    compressed latent (``kv_lora_rank + qk_rope_head_dim``) per layer instead.
    """

    num_hidden_layers: int
    num_key_value_heads: int | None
    head_dim: int | None
    kv_lora_rank: int | None
    qk_rope_head_dim: int | None
    context_length: int | None


def effective_config(config: dict | None) -> dict:
    """Flatten a HuggingFace config so nested language config is reachable.

    Multimodal models (``qwen3_5``, ``kimi_k25``, …) place the transformer shape
    under ``text_config``. Overlaying it on the top level lets every field lookup
    stay flat while top-level keys like ``quantization_config`` remain visible.
    """
    if not config:
        return {}
    text = config.get("text_config")
    if isinstance(text, dict):
        return {**config, **text}
    return config


_NAME_QUANT_TOKENS: dict[str, str] = {
    "fp8": "fp8",
    "fp4": "fp4",
    "mxfp4": "mxfp4",
    "nvfp4": "nvfp4",
    "int4": "int4",
    "int8": "int8",
    "awq": "awq",
    "gptq": "gptq",
    "w8a8": "fp8",
    "w4a16": "int4",
}


def quant_from_name(huggingface_id: str) -> str | None:
    """Infer quantization from a repo name suffix (e.g. ``GLM-4.6-FP8`` → fp8).

    A fallback for repos that ship quantized weights without declaring a
    ``quantization_config`` we recognize. Vendors name these repos by their
    quant, so the trailing token is authoritative.
    """
    leaf = huggingface_id.split("/")[-1].lower()
    for sep in ("-", "_", "."):
        token = leaf.rpartition(sep)[2]
        if token in _NAME_QUANT_TOKENS:
            return _NAME_QUANT_TOKENS[token]
    return None


def _normalize_quant(method: str | None) -> str | None:
    """Map an arbitrary quant-method string to a known precision bucket."""
    if not method:
        return None
    m = method.lower()
    for token in ("mxfp4", "nvfp4", "fp4", "fp8", "int8", "int4", "awq", "gptq"):
        if token in m:
            return token
    if "w8a8" in m:
        return "fp8"
    if "w4a16" in m:
        return "int4"
    return None


def _compressed_tensors_bucket(quant: dict) -> str | None:
    """Derive a precision bucket from a ``compressed-tensors`` quantization config.

    compressed-tensors does not name its scheme; it declares per-group weight
    ``num_bits`` and ``type``. 8-bit float is fp8, 8-bit int is int8, 4-bit is
    int4/fp4.
    """
    groups = quant.get("config_groups")
    if not isinstance(groups, dict):
        return None
    for group in groups.values():
        weights = (group or {}).get("weights") or {}
        num_bits = weights.get("num_bits")
        wtype = (weights.get("type") or "").lower()
        if num_bits == 8:
            return "fp8" if wtype == "float" else "int8"
        if num_bits == 4:
            return "fp4" if wtype == "float" else "int4"
    return None


def native_quant_method(config: dict | None, huggingface_id: str | None = None) -> str | None:
    """Return the repo's native quantization bucket, if it ships pre-quantized.

    A model that is already quantized must not be sized at fp16, which
    overcounts. Resolution order, most authoritative first:

    1. The repo name suffix (e.g. ``GLM-4.6-FP8``) — vendors name by quant.
    2. The declared ``quantization_config`` method, normalized to a bucket.
    3. The ``compressed-tensors`` per-group weight ``num_bits``.

    The returned bucket keys into :data:`BYTES_PER_PARAM`.
    """
    if huggingface_id:
        from_name = quant_from_name(huggingface_id)
        if from_name:
            return from_name
    quant = (config or {}).get("quantization_config") or {}
    method = quant.get("quant_method") or quant.get("quant_type")
    return _normalize_quant(method) or _compressed_tensors_bucket(quant)


def _num_experts(eff: dict) -> int | None:
    return eff.get("num_local_experts") or eff.get("num_experts") or eff.get("n_routed_experts")


def _experts_per_tok(eff: dict) -> int | None:
    return eff.get("num_experts_per_tok") or eff.get("experts_per_token")


def is_moe(config: dict | None) -> bool:
    """Whether the model is a mixture-of-experts (more than one routed expert)."""
    experts = _num_experts(effective_config(config))
    return bool(experts and experts > 1)


def bytes_per_param(quantization: str) -> float:
    """Return bytes-per-weight for a quantization scheme, defaulting to fp16."""
    return BYTES_PER_PARAM.get(quantization.lower(), BYTES_PER_PARAM[DEFAULT_QUANTIZATION])


def extract_shape(config: dict | None) -> ModelShape | None:
    """Pull the KV-cache shape from a HuggingFace ``config.json``.

    Handles nested ``text_config`` and MLA latent attention. Returns ``None`` if
    the config lacks the fields needed to size the KV cache, so callers can fall
    back to a static floor.
    """
    eff = effective_config(config)
    if not eff:
        return None

    num_hidden_layers = eff.get("num_hidden_layers")
    if not num_hidden_layers:
        return None

    kv_lora_rank = eff.get("kv_lora_rank")
    qk_rope_head_dim = eff.get("qk_rope_head_dim")
    context_length = eff.get("max_position_embeddings")

    if kv_lora_rank:
        return ModelShape(
            num_hidden_layers=num_hidden_layers,
            num_key_value_heads=None,
            head_dim=None,
            kv_lora_rank=kv_lora_rank,
            qk_rope_head_dim=qk_rope_head_dim,
            context_length=context_length,
        )

    num_attention_heads = eff.get("num_attention_heads")
    num_key_value_heads = eff.get("num_key_value_heads", num_attention_heads)
    hidden_size = eff.get("hidden_size")
    head_dim = eff.get("head_dim")
    if head_dim is None and hidden_size and num_attention_heads:
        head_dim = hidden_size // num_attention_heads
    if not (num_key_value_heads and head_dim):
        return None

    return ModelShape(
        num_hidden_layers=num_hidden_layers,
        num_key_value_heads=num_key_value_heads,
        head_dim=head_dim,
        kv_lora_rank=None,
        qk_rope_head_dim=None,
        context_length=context_length,
    )


def kv_bytes_per_token(shape: ModelShape) -> float:
    """Bytes of KV cache held per token across all layers.

    MLA stores a single compressed latent per layer (shared across heads);
    standard attention stores key+value for every KV head.
    """
    if shape.kv_lora_rank:
        latent = shape.kv_lora_rank + (shape.qk_rope_head_dim or 0)
        return shape.num_hidden_layers * latent * _KV_BYTES
    return 2 * shape.num_hidden_layers * (shape.num_key_value_heads or 0) * (shape.head_dim or 0) * _KV_BYTES


def derive_active_params(config: dict | None, total_params: int | None) -> int | None:
    """Estimate active (per-token) parameters from a config and total count.

    Dense models activate every weight. For MoE, subtract the FFN parameters of
    the experts that are not routed per token. Returns ``None`` when the MoE FFN
    shape is unavailable to compute the inactive portion.
    """
    if total_params is None:
        return None
    eff = effective_config(config)
    num_experts = _num_experts(eff)
    if not (num_experts and num_experts > 1):
        return total_params
    experts_per_tok = _experts_per_tok(eff)
    hidden_size = eff.get("hidden_size")
    ffn = eff.get("moe_intermediate_size") or eff.get("intermediate_size")
    num_hidden_layers = eff.get("num_hidden_layers")
    if not (hidden_size and ffn and num_hidden_layers and experts_per_tok):
        return None
    ffn_per_expert = 3 * hidden_size * ffn
    inactive_experts = max(num_experts - experts_per_tok, 0)
    inactive_ffn = inactive_experts * ffn_per_expert * num_hidden_layers
    return max(total_params - inactive_ffn, 0)


def estimate_vram(
    *,
    total_params: int,
    shape: ModelShape,
    quantization: str = DEFAULT_QUANTIZATION,
    context_length: int,
    concurrency: int = 1,
    gpu_memory_utilization: float = _DEFAULT_GPU_MEMORY_UTILIZATION,
) -> VramEstimate:
    """Estimate VRAM for one model under a given serving configuration.

    Args:
        total_params: Total parameter count (use total, not active — even MoE
            keeps every expert resident in VRAM).
        shape: Attention shape from :func:`extract_shape` (KV-cache sizing).
        quantization: Weight quantization scheme; see :data:`BYTES_PER_PARAM`.
        context_length: Max sequence length to size the KV cache for.
        concurrency: Number of concurrent full-context sequences to hold.
        gpu_memory_utilization: Fraction of a card the framework can use.

    Returns:
        A :class:`VramEstimate` with each bucket and the total in GiB, plus
        ``required_gb`` (the total grossed up for framework headroom — the number
        to compare against a card's advertised VRAM).
    """
    weights = total_params * bytes_per_param(quantization)
    kv_cache = kv_bytes_per_token(shape) * context_length * concurrency
    overhead = (weights + kv_cache) * _OVERHEAD_FRACTION
    total = weights + kv_cache + overhead

    util = gpu_memory_utilization if gpu_memory_utilization > 0 else _DEFAULT_GPU_MEMORY_UTILIZATION
    required = total / util

    return VramEstimate(
        weights_gb=weights / _GIB,
        kv_cache_gb=kv_cache / _GIB,
        overhead_gb=overhead / _GIB,
        total_gb=total / _GIB,
        required_gb=required / _GIB,
    )


def baseline_min_gpu_memory_gb(
    *,
    total_params: int,
    shape: ModelShape,
    quantization: str = DEFAULT_QUANTIZATION,
) -> int:
    """Compute a conservative ``min_gpu_memory_gb`` floor for an artifact.

    Sizes the model with the given weight quantization (native quant when the
    repo ships pre-quantized), a single sequence, and a context capped at 8K so
    an unusually large declared window doesn't inflate the floor. Rounded up to
    whole GiB.
    """
    sizing_context = min(shape.context_length, 8192) if shape.context_length else 8192
    estimate = estimate_vram(
        total_params=total_params,
        shape=shape,
        quantization=quantization,
        context_length=sizing_context,
        concurrency=1,
    )
    return math.ceil(estimate.required_gb)


MIN_CONTEXT_LENGTH = 4096
"""Floor for a fitted context window — below this, serving is not useful."""


@dataclass(frozen=True)
class ContextFit:
    """Result of fitting a context window to a fixed amount of VRAM.

    Attributes:
        context_length: The largest window that fits, floored at
            :data:`MIN_CONTEXT_LENGTH`.
        required_gb: VRAM the chosen window needs.
        available_gb: VRAM the hardware offers.
        reduced: Whether the window was cut below the model's declared maximum.
        fits: Whether the chosen window actually fits.
    """

    context_length: int
    required_gb: float
    available_gb: float
    reduced: bool
    fits: bool


def fit_context_length(
    *,
    total_params: int,
    shape: ModelShape,
    quantization: str,
    declared_context_length: int,
    concurrency: int,
    available_gb: float,
    gpu_memory_utilization: float = _DEFAULT_GPU_MEMORY_UTILIZATION,
) -> ContextFit:
    """Pick the largest context window that fits, halving down from the declared max.

    Both suggesting a recipe and validating a submitted one size the window this
    way, so the loop lives here next to the estimator rather than being written
    twice with two sets of rounding.

    When nothing fits even at :data:`MIN_CONTEXT_LENGTH`, the floor is returned
    with ``fits=False``: the caller decides whether that is a warning or a hard
    rejection.

    Args:
        total_params: Total parameter count.
        shape: Attention shape from :func:`extract_shape`.
        quantization: Weight quantization the estimate assumes.
        declared_context_length: The model's own maximum window.
        concurrency: Concurrent full-context sequences to hold.
        available_gb: Aggregate VRAM available.
        gpu_memory_utilization: Fraction of a card the framework can use.

    Returns:
        The chosen window and how it relates to the hardware.
    """
    declared = declared_context_length or 8192
    context_length = declared

    while context_length > MIN_CONTEXT_LENGTH:
        estimate = estimate_vram(
            total_params=total_params,
            shape=shape,
            quantization=quantization,
            context_length=context_length,
            concurrency=concurrency,
            gpu_memory_utilization=gpu_memory_utilization,
        )
        if estimate.required_gb <= available_gb:
            return ContextFit(
                context_length=context_length,
                required_gb=estimate.required_gb,
                available_gb=available_gb,
                reduced=context_length < declared,
                fits=True,
            )
        context_length = max(MIN_CONTEXT_LENGTH, context_length // 2)

    estimate = estimate_vram(
        total_params=total_params,
        shape=shape,
        quantization=quantization,
        context_length=MIN_CONTEXT_LENGTH,
        concurrency=concurrency,
        gpu_memory_utilization=gpu_memory_utilization,
    )
    return ContextFit(
        context_length=MIN_CONTEXT_LENGTH,
        required_gb=estimate.required_gb,
        available_gb=available_gb,
        reduced=MIN_CONTEXT_LENGTH < declared,
        fits=estimate.required_gb <= available_gb,
    )
