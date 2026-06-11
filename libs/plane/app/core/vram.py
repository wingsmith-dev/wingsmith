"""VRAM sizing, and whether an artifact fits on a class of card.

Estimation of how much GPU memory a model needs to serve, broken into the four
buckets that actually drive the number: weights, KV cache, a flat runtime
overhead, and the framework's pre-allocation headroom. On top of that, :func:`fit`
answers the question a person actually asks — *can this run on an H100, and how
many* — and :func:`fitment` answers it for every card we know about at once.

This lives in ``core`` rather than in ``modules/workloads`` because it is not about
workloads. A workload is one caller: it declares a device shape and this module says
whether that shape can hold the weights. Registering an artifact is another, and
so is the fitment matrix, which has no workload in it at all. Nothing here touches
the framework or the database; the device catalogue is the one thing it reads,
because a fit is meaningless without knowing how big the card is.

This module is also the single source of truth for interpreting a HuggingFace
``config.json``, which varies sharply across model families: multimodal models
nest the language config under ``text_config``; DeepSeek/GLM/Kimi use MLA
(latent attention) where the KV cache is a compressed latent vector rather than
``num_key_value_heads x head_dim``, and write that latent under two different
names; many modern repos ship natively quantized (``quantization_config``), so
fp16 weight sizing overcounts by up to 4x; and the largest of them are no longer
quantized uniformly at all — an ``expert_dtype`` holds the routed experts at a
narrower precision than the headline one, and on a large MoE the experts are the
checkpoint.
"""

from __future__ import annotations

import math

from dataclasses import dataclass
from collections.abc import Sequence

from app.core.devices import DeviceClass


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

_DEFAULT_GPU_MEMORY_UTILIZATION = 0.9
"""vLLM's default ``--gpu-memory-utilization``: only ~90% of a card is usable.

The single envelope. Everything a serving process holds — weights, KV cache, activation
peak, CUDA context — comes out from under this, which is why nothing here is grossed up
by a second overhead factor on top of it. Doing both was worth about twenty percent, and
twenty percent is a card count.
"""

_FIXED_PER_DEVICE_GB = 1.0
"""CUDA context, captured graphs and allocator slack, per device.

Per *device*, which is the whole point of it being here rather than folded into a
fraction of the weights. This cost is roughly constant per GPU and grows with how many
GPUs a replica uses; it has nothing to do with how big the model is. Sizing it as a
share of the weights made a 2.8T model reserve almost four hundred gigabytes for CUDA
context, which is more than a node.
"""

_ACTIVATION_TENSORS = 4
"""Simultaneously-live intermediates of one batch, as a coarse multiplier.

Deliberately a round number and not a derivation. vLLM sizes the real peak by running a
profiling forward pass, and nothing analytic is going to match that — large-vocabulary
logits during chunked prefill can spike well past this. The goal is to stop being wrong
by a card count, not to reimplement an engine's memory planner, and the first deployment
that fails to start is a better source for this number than any arithmetic here.
"""

DEFAULT_BATCH_TOKENS = 8192
"""Tokens in flight at once when a caller states none — vLLM's own default ballpark.

Sizes the activation peak, and is a different knob from ``concurrency``: concurrency is
how many full-context sequences must be *resident*, this is how many tokens are being
*computed* in one step. A workload carries it as ``max_batch_tokens``.
"""

_DEFAULT_HIDDEN_SIZE = 4096
"""Assumed hidden dimension when a config publishes none, only so activations have a number."""

_GIB = 1024**3
_MIB_PER_GIB = 1024

DEFAULT_QUANTIZATION = "fp16"


@dataclass
class VramEstimate:
    """Per-bucket VRAM breakdown for a single serving configuration, in GiB.

    ``total_gb`` is what the process holds: weights, KV cache and the activation peak.
    ``required_gb`` grosses that up by the utilization envelope to say how much raw card
    it needs — it does not include the per-device fixed reserve, because that depends on
    how many devices the total is spread across and this struct describes one
    configuration rather than one layout. :func:`usable_gb` is the other half.
    """

    weights_gb: float
    kv_cache_gb: float
    activations_gb: float
    total_gb: float
    required_gb: float


@dataclass
class ModelShape:
    """Attention shape needed for KV-cache sizing, pulled from ``config.json``.

    For standard (GQA/MHA) attention, KV is sized from ``num_key_value_heads ×
    head_dim``. For MLA models, ``kv_lora_rank`` is set and the KV cache is the
    compressed latent (``kv_lora_rank + qk_rope_head_dim``) per layer instead.

    ``num_attention_heads`` is carried for sharding rather than for sizing: it is
    the number tensor parallelism has to divide evenly, and a shape that cannot be
    divided is impossible however much memory the cards have.

    ``hidden_size`` sizes the activation peak. It is stored rather than derived from
    heads times head dimension because that identity does not hold for MLA models, where
    ``head_dim`` is absent entirely.

    ``sliding_window`` and ``sliding_layers`` are the interleaved-attention half. A layer
    with a sliding window holds that window and not the sequence, so its share of the
    cache stops growing once the context passes it, and a model where most layers are
    local costs a fraction of what its layer count suggests. A zero ``sliding_layers``
    means every layer is global, which is the ordinary case.

    All of these trail the sizing fields with defaults because they were added after them
    and callers construct this positionally.
    """

    num_hidden_layers: int
    num_key_value_heads: int | None
    head_dim: int | None
    kv_lora_rank: int | None
    qk_rope_head_dim: int | None
    context_length: int | None
    num_attention_heads: int | None = None
    hidden_size: int | None = None
    sliding_window: int | None = None
    sliding_layers: int = 0


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
    3. The declared ``quantization_config`` ``format``.
    4. The ``compressed-tensors`` per-group weight ``num_bits``.

    The config is flattened through :func:`effective_config` first, because a
    multimodal repo nests its language model — and with it its
    ``quantization_config`` — under ``text_config``. Reading only the top level
    saw nothing there and sized a 4-bit model at fp16, a fourfold overcount that
    is a card count rather than a rounding error.

    ``format`` is consulted before the compressed-tensors bit width because
    compressed-tensors names its scheme generically: ``quant_method`` is the
    literal string ``compressed-tensors`` for every bucket it can hold, while
    ``format`` carries the actual one (``mxfp4-pack-quantized``). The bit width is
    the fallback under both, and answers ``fp4`` where ``format`` answers
    ``mxfp4`` — the same bytes per parameter, but only one of them is a string an
    engine accepts as ``--quantization``.

    The returned bucket keys into :data:`BYTES_PER_PARAM`.
    """
    if huggingface_id:
        from_name = quant_from_name(huggingface_id)
        if from_name:
            return from_name
    quant = effective_config(config).get("quantization_config") or {}
    method = quant.get("quant_method") or quant.get("quant_type")
    return (
        _normalize_quant(method)
        or _normalize_quant(quant.get("format"))
        or _compressed_tensors_bucket(quant)
    )


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


def expert_params(config: dict | None) -> int | None:
    """Parameters held by the routed experts, or ``None`` when the MoE shape is absent.

    Every routed expert of every layer, which is the opposite selection to
    :func:`derive_active_params` — that one asks what a *token* touches, this one asks
    what has to be *resident*. On a large MoE they are almost the whole checkpoint:
    DeepSeek-V4-Pro's 384 experts across 61 layers are 1.55 T of its 1.65 T parameters,
    so anything true only of the experts is true of the model.

    Counted from the shape rather than read from a field because no repo publishes it.
    Three matrices per expert (gate, up, down) at ``hidden_size × moe_intermediate_size``,
    which is the same identity :func:`derive_active_params` already uses.
    """
    eff = effective_config(config)
    num_experts = _num_experts(eff)
    hidden_size = eff.get("hidden_size")
    ffn = eff.get("moe_intermediate_size")
    layers = eff.get("num_hidden_layers")
    if not (num_experts and num_experts > 1 and hidden_size and ffn and layers):
        return None
    return num_experts * 3 * hidden_size * ffn * layers


def expert_quant_method(config: dict | None) -> str | None:
    """The precision the routed experts are stored at, when the repo declares one.

    A checkpoint is not obliged to hold every tensor at one precision, and the large
    ones no longer do. DeepSeek-V4-Pro declares ``quantization_config.quant_method:
    fp8`` and ``expert_dtype: fp4``: the dense trunk is 8-bit, the experts are 4-bit,
    and the experts are 94% of the parameters. Sizing all 1.65 T at the headline fp8
    charged 1,537 GiB for a checkpoint that is about 960 GB on disk — over by 1.6x,
    which put the model on two H200 nodes and refused the one node vLLM publishes a
    recipe for.

    Returns ``None`` when nothing is declared, which means "the same as everything
    else" and leaves the single-precision sizing exactly as it was. A declared dtype
    that is *wider* than the headline (bf16 experts under an fp8 trunk) is read the
    same way and makes the estimate larger, because the field says what is stored and
    not which direction it is convenient to round.
    """
    declared = str(effective_config(config).get("expert_dtype") or "").strip().lower()
    if not declared:
        return None
    if declared in BYTES_PER_PARAM:
        return declared
    return _normalize_quant(declared)


_SLIDING_LAYER = "sliding_attention"
"""How `layer_types` names a local layer. Anything else in that list is global."""


def _sliding(eff: dict, num_hidden_layers: int) -> tuple[int | None, int]:
    """The window a config declares, and how many of its layers actually use one.

    Interleaved attention is not a detail. Gemma alternates five local layers to one
    global and Gemma 3's window is 1024 tokens, so at a declared 131k window the local
    layers hold about a hundredth of what a full-attention sizing charges them — and
    charging them anyway is what put gemma-4-26b at 122 GiB, spread it over two cards
    it does not need, and then refused the one card it does. gpt-oss, Ministral and
    Qwen's hybrids are all the same shape of wrong.

    Two ways a repo says it, and the explicit one wins. `layer_types` lists every layer
    by name and is what current Transformers writes; `sliding_window_pattern` is the
    older form, an integer N meaning one global layer in every N. A window with neither
    is a model where *every* layer is local, which is what Mistral's original sliding
    window was.

    `use_sliding_window: false` is a repo that ships the parameters and has turned the
    behaviour off, and it is read as off. Reading the window regardless would size a
    Qwen3 as a hybrid on the strength of a field it does not use.
    """
    window = eff.get("sliding_window")
    if not window or eff.get("use_sliding_window") is False:
        return None, 0

    types = eff.get("layer_types")
    if isinstance(types, list) and types:
        return int(window), sum(1 for kind in types if kind == _SLIDING_LAYER)

    pattern = eff.get("sliding_window_pattern")
    if isinstance(pattern, int) and pattern > 1:
        globals_ = math.ceil(num_hidden_layers / pattern)
        return int(window), max(0, num_hidden_layers - globals_)

    return int(window), num_hidden_layers


def _latent_rank(eff: dict) -> int | None:
    """The compressed KV latent width, under either spelling a repo uses for it.

    ``kv_lora_rank`` is DeepSeek's own name and is what V3, R1 and GLM publish.
    Transformers 4.57 writes the same tensor as ordinary attention fields instead —
    a single KV head whose ``head_dim`` *is* the whole latent — and emits no
    ``kv_lora_rank`` at all. DeepSeek-V4-Pro is written that second way.

    Reading only the first name dropped V4-Pro into the standard branch and sized its
    cache as separate key and value tensors across a 512-wide head: 2 × 61 × 512 × 2
    bytes per token, against the 61 × (512 + 64) × 2 that latent attention actually
    holds. A 1.78x overcount on the term that decides how much context a node buys,
    and it also lost ``qk_rope_head_dim`` entirely.

    ``qk_rope_head_dim`` is the discriminator, because it exists only on latent
    attention. A config carrying it alongside exactly one KV head is compressed
    attention however it spelled the rank; a config without it is ordinary GQA and
    keeps the key-plus-value sizing it deserves.
    """
    declared = eff.get("kv_lora_rank")
    if declared:
        return int(declared)
    if not eff.get("qk_rope_head_dim") or eff.get("num_key_value_heads") != 1:
        return None
    head_dim = eff.get("head_dim")
    return int(head_dim) if head_dim else None


def extract_shape(config: dict | None) -> ModelShape | None:
    """Pull the KV-cache shape from a HuggingFace ``config.json``.

    Handles nested ``text_config`` and MLA latent attention. Returns ``None`` if
    the config lacks the fields needed to size the KV cache, so callers can fall
    back to a static floor.

    A latent-attention model is sized at full attention and never reads
    ``sliding_window``, which is deliberate rather than an omission. DeepSeek-V4-Pro
    declares ``sliding_window: 128`` next to an ``index_topk`` — that pair is sparse
    *selection*, not a smaller cache: the whole latent stays resident and the indexer
    picks which of it to attend to. Read as an interleaved window with no
    ``layer_types`` to contradict it, the standard branch charged all 61 layers 128
    tokens each and produced a KV cache of 0.015 GiB for an 800k window, understating
    it by three orders of magnitude. Detecting the latent first is what stops that.
    """
    eff = effective_config(config)
    if not eff:
        return None

    num_hidden_layers = eff.get("num_hidden_layers")
    if not num_hidden_layers:
        return None

    sliding_window, sliding_layers = _sliding(eff, num_hidden_layers)

    kv_lora_rank = _latent_rank(eff)
    qk_rope_head_dim = eff.get("qk_rope_head_dim")
    context_length = eff.get("max_position_embeddings")
    num_attention_heads = eff.get("num_attention_heads")
    declared_hidden = eff.get("hidden_size")

    if kv_lora_rank:
        return ModelShape(
            num_hidden_layers=num_hidden_layers,
            num_key_value_heads=None,
            head_dim=None,
            kv_lora_rank=kv_lora_rank,
            qk_rope_head_dim=qk_rope_head_dim,
            context_length=context_length,
            num_attention_heads=num_attention_heads,
            hidden_size=declared_hidden,
        )

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
        num_attention_heads=num_attention_heads,
        hidden_size=declared_hidden,
        sliding_window=sliding_window,
        sliding_layers=sliding_layers,
    )


def kv_bytes_per_token(shape: ModelShape) -> float:
    """Bytes of KV cache held per token across all layers, at full attention.

    MLA stores a single compressed latent per layer (shared across heads);
    standard attention stores key+value for every KV head.

    Full attention, which is what every layer of most models does. A model with
    interleaved local layers holds less than this and :func:`kv_cache_bytes` is what
    knows so — this is the per-layer rate that one multiplies, kept as its own function
    because it is the figure that does not depend on how long the sequence is.
    """
    if shape.kv_lora_rank:
        latent = shape.kv_lora_rank + (shape.qk_rope_head_dim or 0)
        return shape.num_hidden_layers * latent * _KV_BYTES
    return 2 * shape.num_hidden_layers * (shape.num_key_value_heads or 0) * (shape.head_dim or 0) * _KV_BYTES


def kv_cache_bytes(shape: ModelShape, context_length: int) -> float:
    """Bytes of KV cache one sequence of ``context_length`` tokens holds.

    Not ``kv_bytes_per_token × context_length``, because that identity only holds when
    every layer attends to the whole sequence. A layer with a sliding window holds the
    window, so its cost stops growing at the window and the model's total is two terms
    rather than one product.

    The difference is not a refinement. Gemma runs five local layers of 1024 tokens to
    every global one; sized as if all sixty-two were global, gemma-4-26b came out at
    122 GiB for a declared 131k window — which spread it across two cards, then produced
    a per-device floor larger than the single 96 GiB card it actually serves on, and the
    plane refused to place a workload that would have run.

    Per sequence and per whole model, ahead of any tensor split: what a device holds is
    this divided by the parallel degree, and the callers that care do that division.
    """
    per_layer = kv_bytes_per_token(shape) / max(1, shape.num_hidden_layers)
    local = min(shape.sliding_layers, shape.num_hidden_layers) if shape.sliding_window else 0
    resident = min(context_length, shape.sliding_window) if shape.sliding_window else context_length
    return per_layer * ((shape.num_hidden_layers - local) * context_length + local * resident)


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


def hidden_size_of(shape: ModelShape) -> int:
    """The hidden dimension, declared where possible and reconstructed where not.

    ``num_attention_heads × head_dim`` is the identity for standard attention and is a
    fair reconstruction when a config omits ``hidden_size``; it does not hold for MLA,
    where ``head_dim`` is absent, which is why the declared value is preferred.
    """
    if shape.hidden_size:
        return shape.hidden_size
    if shape.num_attention_heads and shape.head_dim:
        return shape.num_attention_heads * shape.head_dim
    return _DEFAULT_HIDDEN_SIZE


def activation_bytes(shape: ModelShape, batch_tokens: int) -> float:
    """The activation peak of one forward step, coarsely.

    Scales with tokens in flight and the hidden dimension, and with nothing else — in
    particular not with the parameter count, which is what the flat overhead fraction
    this replaced got wrong. A 2.8T model and a 27B model running the same batch hold
    activation tensors of the same shape; only their weights differ.

    See :data:`_ACTIVATION_TENSORS` for why the multiplier is a round number.
    """
    return batch_tokens * hidden_size_of(shape) * _KV_BYTES * _ACTIVATION_TENSORS


def usable_gb(
    memory_mib: int,
    device_count: int,
    gpu_memory_utilization: float = _DEFAULT_GPU_MEMORY_UTILIZATION,
) -> float:
    """What a set of devices actually offers a serving process, in GiB.

    The capacity half, kept apart from :func:`estimate_vram`'s demand half because the
    per-device reserve depends on the layout rather than on the model. Floored at zero so
    a card smaller than the fixed reserve reads as offering nothing instead of as owing
    memory.
    """
    util = gpu_memory_utilization if gpu_memory_utilization > 0 else _DEFAULT_GPU_MEMORY_UTILIZATION
    per_device = memory_mib / _MIB_PER_GIB * util - _FIXED_PER_DEVICE_GB
    return max(0.0, device_count * per_device)


def estimate_vram(
    *,
    total_params: int,
    shape: ModelShape,
    quantization: str = DEFAULT_QUANTIZATION,
    context_length: int,
    concurrency: int = 1,
    batch_tokens: int = DEFAULT_BATCH_TOKENS,
    gpu_memory_utilization: float = _DEFAULT_GPU_MEMORY_UTILIZATION,
    expert_params: int | None = None,
    expert_quantization: str | None = None,
) -> VramEstimate:
    """Estimate VRAM for one model under a given serving configuration.

    Three buckets, each with its own shape: weights scale with parameters, the KV cache
    with resident tokens, activations with tokens in flight. They are not grossed up by
    an overhead fraction — the utilization envelope is the only discount, and applying
    both double-counted the same headroom twice.

    Args:
        total_params: Total parameter count (use total, not active — even MoE
            keeps every expert resident in VRAM).
        shape: Attention shape from :func:`extract_shape` (KV-cache sizing).
        quantization: Weight quantization scheme; see :data:`BYTES_PER_PARAM`.
        context_length: Max sequence length to size the KV cache for.
        concurrency: Number of concurrent full-context sequences to hold.
        batch_tokens: Tokens computed in one step, which sizes the activation peak.
        gpu_memory_utilization: Fraction of a card the framework can use.
        expert_params: Parameters held at a second precision; see :class:`Sizing`.
        expert_quantization: The precision those are held at.

    Returns:
        A :class:`VramEstimate` with each bucket and the total in GiB, plus
        ``required_gb`` — the total grossed up by the envelope, which is the raw card
        capacity needed before the per-device reserve in :func:`usable_gb`.
    """
    weights = _weight_bytes(total_params, quantization, expert_params, expert_quantization)
    kv_cache = kv_cache_bytes(shape, context_length) * concurrency
    activations = activation_bytes(shape, batch_tokens)
    total = weights + kv_cache + activations

    util = gpu_memory_utilization if gpu_memory_utilization > 0 else _DEFAULT_GPU_MEMORY_UTILIZATION

    return VramEstimate(
        weights_gb=weights / _GIB,
        kv_cache_gb=kv_cache / _GIB,
        activations_gb=activations / _GIB,
        total_gb=total / _GIB,
        required_gb=total / util / _GIB,
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
    batch_tokens: int = DEFAULT_BATCH_TOKENS,
    expert_params: int | None = None,
    expert_quantization: str | None = None,
) -> ContextFit:
    """Pick the largest context window that fits, halving down from the declared max.

    Both suggesting a configuration and validating a submitted one size the window this
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
        available_gb: What the devices actually offer, from :func:`usable_gb`. Already net
            of the utilization envelope and the per-device reserve, so nothing here
            discounts it again.
        batch_tokens: Tokens computed in one step, which sizes the activation peak.
        expert_params: Parameters held at a second precision; see :class:`Sizing`.
        expert_quantization: The precision those are held at.

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
            batch_tokens=batch_tokens,
            expert_params=expert_params,
            expert_quantization=expert_quantization,
        )
        if estimate.total_gb <= available_gb:
            return ContextFit(
                context_length=context_length,
                required_gb=estimate.total_gb,
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
        batch_tokens=batch_tokens,
        expert_params=expert_params,
        expert_quantization=expert_quantization,
    )
    return ContextFit(
        context_length=MIN_CONTEXT_LENGTH,
        required_gb=estimate.total_gb,
        available_gb=available_gb,
        reduced=MIN_CONTEXT_LENGTH < declared,
        fits=estimate.total_gb <= available_gb,
    )


DEFAULT_DEVICES_PER_NODE = 8
"""Devices in one node when nothing has said otherwise — an HGX chassis.

"Node" here means a set of devices sharing a coherent interconnect, which is what makes
it the unit a replica is laid out in. On an HGX board that is the chassis and the number
is eight. On a GB300 NVL72 it is the whole rack and the number is seventy-two — the rack
is many chassis of silicon and exactly one node to anything scheduling on it, which is
the point of the interconnect.

`supply_topology_domain.device_count` is the real answer wherever a cluster has reported
one, and this is only what to assume for hardware nobody has racked yet. It is not a
ceiling: a replica spans nodes over the backend fabric whenever it needs to.
"""

MAX_DEVICES = 512
"""A runaway bound on a fit, deliberately far above any layout worth proposing.

Not a claim that 512 devices is servable. It exists so a fit against an absurd
artifact terminates rather than walking powers of two forever, and it is set where it
cannot be mistaken for a policy about how wide a replica should be.
"""


@dataclass(frozen=True)
class Layout:
    """How a device count is arranged: tensor-parallel within a node, pipeline across.

    The two axes are different hardware. Tensor parallelism is bandwidth-hungry and
    latency-critical, so it stays inside one node, where every device shares a coherent
    interconnect. Pipeline parallelism passes activations between stages, which the
    backend fabric carries perfectly well — so spanning nodes is a normal thing to
    propose rather than a last resort, and the nodes a replica spans are one
    fabric-connected unit rather than a set of islands.

    That is why a fit no longer stops at one node. It used to, on the reasoning that
    spanning was a promise the plane could not keep. With a backend fabric it plainly
    can, and the ceiling meant nothing larger than a single chassis of VRAM could ever
    be reported as fitting at all — which is wrong for every frontier model there is.
    """

    device_count: int
    node_count: int
    tensor_parallel: int
    pipeline_parallel: int

    @property
    def spans_nodes(self) -> bool:
        return self.node_count > 1


def layout_for(device_count: int, devices_per_node: int = DEFAULT_DEVICES_PER_NODE) -> Layout:
    """Split a device count into tensor and pipeline degrees for a given node size.

    Tensor parallelism takes the largest power of two that both divides the count and
    fits inside one node; pipeline parallelism takes what is left. Powers of two because
    that is what the engines accept, and capped at the node because a tensor group
    reaching over the backend fabric is the one layout that genuinely does not work.

    A node size that is not a power of two — seventy-two, say — is handled by the cap
    rather than by arithmetic on it: sixteen devices inside an NVL72 are one node and
    tensor-parallel sixteen, which is the answer somebody with that rack wants.
    """
    ceiling = max(1, min(device_count, devices_per_node))
    tensor = 1
    while tensor * 2 <= ceiling and device_count % (tensor * 2) == 0:
        tensor *= 2
    return Layout(
        device_count=device_count,
        node_count=math.ceil(device_count / max(1, devices_per_node)),
        tensor_parallel=tensor,
        pipeline_parallel=device_count // tensor,
    )


def device_counts(max_devices: int = MAX_DEVICES) -> list[int]:
    """The device counts a fit will try, ascending.

    Powers of two, because tensor parallelism wants an even split and racks are built
    in them, so a count of five is arithmetically correct and cannot be served.
    """
    counts: list[int] = []
    count = 1
    while count <= max(1, max_devices):
        counts.append(count)
        count *= 2
    return counts


@dataclass(frozen=True)
class Sizing:
    """What an artifact contributes to a fit: how big it is, and at what precision.

    Kept apart from :class:`ModelShape` because those three facts arrive from three
    places — the parameter count from the Hub's model index, the attention shape
    from ``config.json``, and the quantization from whoever is serving it, who is
    entitled to differ from the repo. Bundling them is what lets a fit be computed
    from a stored artifact without reaching back to HuggingFace for anything.

    ``shape`` is optional, and that is the difference between a partial answer and no
    answer. The attention shape is needed for one thing — sizing the KV cache — and
    the parameter count alone still settles how much has to be resident, which is the
    half with no fudge factor in it. An artifact that publishes a size but no config
    can still be told which cards are too small; refusing to fit it at all would be
    withholding the certain half because the uncertain half is missing.
    """

    total_params: int
    shape: ModelShape | None
    quantization: str = DEFAULT_QUANTIZATION
    expert_params: int | None = None
    expert_quantization: str | None = None
    """How much of the checkpoint is held at a second precision, and which.

    Both unset is the ordinary case and sizes exactly as one precision always did.
    Both set says the routed experts are stored differently from the rest — see
    :func:`expert_quant_method` for why a repo does that and what it cost to ignore.
    They trail the other fields with defaults because callers construct this
    positionally.
    """


@dataclass(frozen=True)
class Intent:
    """What somebody wants to serve, independent of what they want to serve it on.

    The separation is the point: an intent is stated once and then fitted against
    every card in the catalogue, which is what makes the answers comparable. A
    context window fitted per-card would compare a full window on one class against
    a truncated one on another and call them both a fit.
    """

    context_length: int
    concurrency: int = 1
    gpu_memory_utilization: float = _DEFAULT_GPU_MEMORY_UTILIZATION
    batch_tokens: int = DEFAULT_BATCH_TOKENS
    devices_per_node: int = DEFAULT_DEVICES_PER_NODE
    max_devices: int = MAX_DEVICES


@dataclass(frozen=True)
class Fit:
    """Whether one artifact serves on one class of card, and what it costs to.

    A no-fit is as informative as a fit and carries the same numbers, because
    "needs more than 8× L4" is the answer to a procurement question and "doesn't
    fit" is not. ``reason`` is a sentence for a person, always populated.

    ``reduced`` marks a fit that had to cut the context window below what was
    asked. It is deliberately not folded into ``fits``: serving a 131k model at 32k
    is a real option someone may take knowingly, and a view that showed it as a
    failure would hide the cheapest card that works.

    ``kv_counted`` is false when the artifact published no attention shape, so
    ``required_gb`` is the weights and nothing else. Such a fit is a floor rather
    than an estimate — the real figure is strictly larger — and a caller that
    presented it as a full sizing would be promising a window nobody costed.
    ``context_length`` is zero on those, because no window was priced.

    ``possible`` is the certain half of ``fits``, and the two are different questions
    with different consequences. ``possible`` is arithmetic — the weights sit on the
    cards, the heads divide across the tensor group — and a caller may refuse over it.
    ``fits`` adds the estimated half, the window and the batch, and a caller that
    refuses over *that* eventually refuses a configuration that serves: a row can be
    ``possible`` and not ``fits``, and it is the one somebody with an argument should
    still be able to choose.

    ``node_count`` is the answer people actually act on — "four nodes of B300" is an
    order, "thirty-two devices" is a number to divide. Reporting only the total made
    every reader do that division, and getting it wrong is how somebody buys one chassis
    for a model that needs four.
    """

    device_class: str
    fits: bool
    possible: bool
    device_count: int
    node_count: int
    tensor_parallel: int
    pipeline_parallel: int
    memory_mib_per_device: int
    aggregate_gb: float
    weights_gb: float
    required_gb: float
    context_length: int
    reduced: bool
    kv_counted: bool
    reason: str


@dataclass(frozen=True)
class WeightTerm:
    """One precision's share of the checkpoint, named so a refusal can be argued with.

    A refusal that says "the weights need 1,537 GB at fp8" is unarguable when the
    real answer is that 94% of those parameters are 4-bit. Splitting the term is what
    lets the sentence carry its own working.
    """

    label: str
    params: int
    quantization: str
    gb: float


def weight_terms(sizing: Sizing) -> tuple[WeightTerm, ...]:
    """The weights split by the precision each part is actually stored at.

    One term for a checkpoint held at one precision, which is nearly all of them.
    Two when :class:`Sizing` carries an expert precision: the routed experts at
    theirs, everything else at the headline one. The expert count is clamped to the
    total, because it is derived from the MoE shape and a shape that disagrees with
    the published parameter count must not produce negative weights.
    """
    total = max(0, sizing.total_params)
    experts = min(max(0, sizing.expert_params or 0), total)
    if not experts or not sizing.expert_quantization:
        return (
            WeightTerm(
                label="weights",
                params=total,
                quantization=sizing.quantization,
                gb=total * bytes_per_param(sizing.quantization) / _GIB,
            ),
        )
    return (
        WeightTerm(
            label="dense weights",
            params=total - experts,
            quantization=sizing.quantization,
            gb=(total - experts) * bytes_per_param(sizing.quantization) / _GIB,
        ),
        WeightTerm(
            label="routed experts",
            params=experts,
            quantization=sizing.expert_quantization,
            gb=experts * bytes_per_param(sizing.expert_quantization) / _GIB,
        ),
    )


def weights_gb(sizing: Sizing) -> float:
    """Resident weight bytes in GiB. Arithmetic, not an estimate.

    Every other number this module produces has a fudge factor in it. This one
    does not: the weights are on the cards before a single token is served, so a
    shape that cannot hold them is impossible rather than optimistic. That makes
    it the only VRAM fact worth refusing somebody's configuration over — and the
    reason a mixed-precision checkpoint has to be summed per precision rather than
    charged at whichever one the repo put in its headline.
    """
    return sum(term.gb for term in weight_terms(sizing))


def _weight_bytes(
    total_params: int,
    quantization: str,
    expert_params: int | None,
    expert_quantization: str | None,
) -> float:
    """:func:`weights_gb` in bytes, for the callers that hold the parts and not a ``Sizing``."""
    return (
        weights_gb(
            Sizing(
                total_params=total_params,
                shape=None,
                quantization=quantization,
                expert_params=expert_params,
                expert_quantization=expert_quantization,
            )
        )
        * _GIB
    )


def _shardable_heads(shape: ModelShape | None, device_count: int) -> bool:
    """Whether tensor parallelism at this width divides the attention heads evenly.

    A fit assumes pure tensor parallelism within a node, so the device count *is*
    the TP degree. Every engine we run splits attention heads across that degree
    and wants the split to come out whole — 28 heads over 8 devices is a layout no
    amount of memory makes possible. A config that does not publish a head count
    is not contradicted, only unchecked, and neither is an artifact with no shape
    at all.
    """
    heads = shape.num_attention_heads if shape is not None else None
    if not heads or device_count <= 1:
        return True
    return heads % device_count == 0


def impossible(
    sizing: Sizing,
    device: DeviceClass,
    device_count: int,
    tensor_parallel: int | None = None,
    gpu_memory_utilization: float = _DEFAULT_GPU_MEMORY_UTILIZATION,
) -> str | None:
    """Why this shape cannot work, or ``None`` when it might.

    The certain half of a fit, split out so that a caller can refuse a shape
    without refusing it over an estimate. Two rules, both arithmetic: tensor
    parallelism has to divide the attention heads evenly, and the weights have to
    be resident before an engine serves a token. Neither has a fudge factor in it,
    so failing either is impossible rather than merely unlikely.

    Everything a fit knows beyond this — context window, batch, KV cache — rests on
    a flat 15% overhead guess. That is advice, and callers that enforce it will
    eventually refuse a configuration that works.

    A class the catalogue carries no memory figure for is not contradicted: an
    unmeasured card returns ``None`` rather than a refusal nobody can act on.

    ``tensor_parallel`` is what the heads have to divide across, and it is not the
    device count once a replica spans nodes — sixteen devices as tensor-eight by
    pipeline-two asks the heads to divide by eight. Defaulting it to the count is right
    only for a single-node layout, so callers with a layout pass theirs.
    """
    if device.memory_mib <= 0:
        return None
    tensor = device_count if tensor_parallel is None else tensor_parallel
    if sizing.shape is not None and not _shardable_heads(sizing.shape, tensor):
        return (
            f"{sizing.shape.num_attention_heads} attention heads do not divide evenly "
            f"across a tensor-parallel group of {tensor}"
        )
    weights = weights_gb(sizing)
    offered = usable_gb(device.memory_mib, device_count, gpu_memory_utilization)
    if weights > offered:
        return (
            f"the weights need {weights:,.0f} GB ({_weights_words(sizing)}) and {device_count}× "
            f"{device.label} offers {offered:,.0f} GB usable"
        )
    return None


def _weights_words(sizing: Sizing) -> str:
    """The weight terms as a person can check them: how many parameters, at what precision.

    The whole of why :func:`weight_terms` exists. A refusal that names only a total and
    a headline precision cannot be argued with by somebody who knows the checkpoint is
    mixed; one that says "1.5 T routed experts at fp4, 103 B dense at fp8" can be
    checked against the repo in a minute.
    """
    return ", ".join(f"{term.params / 1e9:,.0f}B {term.label} at {term.quantization}" for term in weight_terms(sizing))


def _devices_words(layout: Layout, device: DeviceClass, devices_per_node: int) -> str:
    """The layout as a person would order it: nodes first, devices as the total.

    "32 devices" is a number to divide; "4 nodes of 8× B300" is a thing to buy. An NVL72
    rack is one node of seventy-two by the same rule, because the interconnect is what
    makes a node a node and the rack has one.
    """
    if not layout.spans_nodes:
        return f"{layout.device_count}× {device.label} in one node"
    per_node = min(devices_per_node, layout.device_count)
    return f"{layout.node_count} nodes of {per_node}× {device.label} ({layout.device_count} devices)"


def _parallel_words(layout: Layout) -> str:
    """How the layout is split, said only when it is split more than one way."""
    if layout.pipeline_parallel <= 1:
        return f"tensor-parallel {layout.tensor_parallel}"
    return f"tensor-parallel {layout.tensor_parallel} across pipeline-parallel {layout.pipeline_parallel}"


def fit_at(sizing: Sizing, device: DeviceClass, intent: Intent, device_count: int) -> Fit:
    """Fit an artifact onto an exact number of cards of one class.

    Two gates in order, because the first makes the second meaningful: whatever
    :func:`impossible` rules out is ruled out, and only then is there a question
    about how much context the remaining memory buys.

    A class the catalogue carries no memory figure for cannot be fitted against and
    comes back as a no-fit saying so, rather than as a division by an unknown.

    With no attention shape there is no second gate: the weights either sit on the
    cards or they do not, and the answer comes back with ``kv_counted`` false to say
    the number is a floor rather than an estimate.

    The count is laid out across nodes before anything is checked, because the tensor
    degree that the attention heads have to divide across is a property of the layout
    and not of the total.
    """
    aggregate_gb = usable_gb(device.memory_mib, device_count, intent.gpu_memory_utilization)
    """What the devices offer, not what the box says on the lid. Net of the utilization
    envelope and the per-device reserve, so every comparison below is against memory a
    serving process can actually have."""
    weights = weights_gb(sizing)
    layout = layout_for(device_count, intent.devices_per_node)
    counted = sizing.shape is not None
    """Whether any verdict off this sizing can have priced a window. A property of the
    artifact rather than of the branch that returns — a shapeless no-fit is as
    weights-only as a shapeless fit, and defaulting it to true made the refusals lie."""

    def verdict(
        *,
        fits: bool,
        context_length: int,
        reduced: bool,
        required_gb: float,
        reason: str,
        kv_counted: bool | None = None,
        possible: bool | None = None,
    ) -> Fit:
        return Fit(
            device_class=device.name,
            fits=fits,
            possible=fits if possible is None else possible,
            device_count=device_count,
            node_count=layout.node_count,
            tensor_parallel=layout.tensor_parallel,
            pipeline_parallel=layout.pipeline_parallel,
            memory_mib_per_device=device.memory_mib,
            aggregate_gb=aggregate_gb,
            weights_gb=weights,
            required_gb=required_gb,
            context_length=context_length,
            reduced=reduced,
            kv_counted=counted if kv_counted is None else kv_counted,
            reason=reason,
        )

    if device.memory_mib <= 0:
        return verdict(
            fits=False,
            possible=False,
            context_length=intent.context_length,
            reduced=False,
            required_gb=0.0,
            reason=f"the catalogue carries no memory size for {device.name}, so it cannot be fitted against",
        )

    blocked = impossible(
        sizing,
        device,
        device_count,
        tensor_parallel=layout.tensor_parallel,
        gpu_memory_utilization=intent.gpu_memory_utilization,
    )
    if blocked is not None:
        return verdict(
            fits=False,
            possible=False,
            context_length=intent.context_length,
            reduced=False,
            required_gb=weights,
            reason=blocked,
        )

    words = _devices_words(layout, device, intent.devices_per_node)
    if sizing.shape is None:
        return verdict(
            fits=True,
            context_length=0,
            reduced=False,
            required_gb=weights,
            kv_counted=False,
            reason=(
                f"needs {words} to hold {weights:,.0f} GB of weights in {aggregate_gb:,.0f} GB usable, "
                f"{_parallel_words(layout)}; no context window is costed, so the real "
                f"requirement is higher"
            ),
        )

    fitted = fit_context_length(
        total_params=sizing.total_params,
        shape=sizing.shape,
        quantization=sizing.quantization,
        declared_context_length=intent.context_length,
        concurrency=intent.concurrency,
        available_gb=aggregate_gb,
        batch_tokens=intent.batch_tokens,
        expert_params=sizing.expert_params,
        expert_quantization=sizing.expert_quantization,
    )
    if not fitted.fits:
        return verdict(
            fits=False,
            possible=True,
            context_length=fitted.context_length,
            reduced=fitted.reduced,
            required_gb=fitted.required_gb,
            reason=(
                f"{words} holds the weights but not a usable context: "
                f"{fitted.context_length:,} tokens needs {fitted.required_gb:,.0f} GB of {aggregate_gb:,.0f} GB"
            ),
        )
    window = (
        f"at {fitted.context_length:,} tokens rather than the {intent.context_length:,} asked for"
        if fitted.reduced
        else f"at the full {fitted.context_length:,} tokens"
    )
    return verdict(
        fits=True,
        context_length=fitted.context_length,
        reduced=fitted.reduced,
        required_gb=fitted.required_gb,
        reason=f"needs {words} {window}, {_parallel_words(layout)}",
    )


def fit(sizing: Sizing, device: DeviceClass, intent: Intent) -> Fit:
    """The smallest number of cards of one class that serves this artifact.

    Preference order is deliberate. The smallest count carrying the full asked-for
    context wins; failing that, the smallest count carrying any usable context, so
    a truncated window is offered rather than hidden; failing that, the largest
    attempt is returned as the no-fit, because how far off it was is the useful
    part of a refusal.

    The ladder runs to :data:`MAX_DEVICES` and not to one node. A 2.8T model does not fit
    in any single HGX chassis on the market, and stopping at eight devices meant reporting
    every frontier model as unservable on hardware that serves it.
    """
    attempts: list[Fit] = []
    for device_count in device_counts(intent.max_devices):
        attempt = fit_at(sizing, device, intent, device_count)
        if attempt.fits and not attempt.reduced:
            return attempt
        attempts.append(attempt)
    for attempt in attempts:
        if attempt.fits:
            return attempt
    return attempts[-1]


def ladder(sizing: Sizing, device: DeviceClass, intent: Intent) -> list[Fit]:
    """Every count of one class worth offering, ascending, not only the smallest.

    :func:`fit` answers "what is the least hardware this needs", which is one answer to a
    question that has several. A 70B on H200 is a fit at four cards with a trimmed window
    and a fit at eight with the full one, and both are things somebody deploys on
    purpose: four leaves half the node for another workload, eight buys context and
    concurrency. Returning only the recommendation made the second half of that choice
    invisible, and the console then had nothing to offer but the whole node.

    Counts below the recommendation are here because they are the interesting ones — the
    recommendation is the smallest count at the *full* window, so anything smaller that
    still serves is a deliberate trade of context for cards. Counts above it stop at one
    node, or at the recommendation when that already spans several: widening a replica
    past the fabric to buy KV cache is a throughput decision nothing here can price, and
    it is not a menu item.

    No-fits are dropped rather than listed. This is a set of choices, and a count that
    cannot hold the weights is not one; :func:`fit` still explains a class that fits
    nowhere, and it is the row's own verdict that says so.
    """
    chosen = fit(sizing, device, intent)
    if not chosen.fits:
        return []
    ceiling = min(intent.max_devices, max(chosen.device_count, intent.devices_per_node))
    options = [fit_at(sizing, device, intent, count) for count in device_counts(ceiling)]
    return [option for option in options if option.fits]


def fitment(sizing: Sizing, devices: Sequence[DeviceClass], intent: Intent) -> list[Fit]:
    """Fit an artifact against many classes at once, most useful answer first.

    The ordering is the answer to "what should I ask for": fits before no-fits,
    full context before truncated, then fewest devices, then least aggregate
    memory. A class that works on four cards ranks above one that works on eight,
    and among equals the smaller card wins — the larger one is capacity somebody
    else could have had.
    """
    results = [fit(sizing, device, intent) for device in devices]
    results.sort(
        key=lambda f: (not f.fits, f.reduced, f.device_count, f.aggregate_gb, f.device_class),
    )
    return results


SHAPE_FIELDS = (
    "num_hidden_layers",
    "num_key_value_heads",
    "head_dim",
    "kv_lora_rank",
    "qk_rope_head_dim",
    "context_length",
    "num_attention_heads",
    "hidden_size",
    "sliding_window",
    "sliding_layers",
)


def shape_metadata(shape: ModelShape) -> dict:
    """The attention shape as it is persisted on an artifact.

    Sizing an artifact after registration has to work without the network. The
    derived scalars an artifact already carries — context length, quantization,
    parameter count — are not enough to size a KV cache, so the shape itself is
    written down at resolve time and read back by :func:`sizing_of`.
    """
    return {name: getattr(shape, name) for name in SHAPE_FIELDS}


def sizing_of(metadata: dict, parameter_count: int | None) -> Sizing | None:
    """Rebuild a :class:`Sizing` from a stored artifact, or ``None`` if it cannot be.

    The parameter count is the only hard requirement. Without it there is no sizing
    of any kind and this returns ``None``; with it and nothing else, the result
    carries no shape and fits on weights alone — a floor, which is a poorer answer
    than a full estimate and a far better one than a blank panel.

    That distinction matters because the missing-shape case is not rare. Every
    artifact registered before the shape was persisted has one, as does every
    artifact adopted by watching a cluster serve weights nobody resolved.

    A stored shape from before the sliding-window fields existed reads back as full
    attention, which is the safe direction — it over-states the cache rather than
    under-stating it — but for an interleaved model it over-states it by a lot, and
    re-reading the source is what corrects the record.
    """
    if not parameter_count:
        return None
    quantization = metadata.get("quantization") or DEFAULT_QUANTIZATION
    expert_params = metadata.get("expert_parameter_count")
    expert_quantization = metadata.get("expert_quantization")
    stored = metadata.get("shape")
    if not isinstance(stored, dict) or not stored.get("num_hidden_layers"):
        return Sizing(
            total_params=parameter_count,
            shape=None,
            quantization=quantization,
            expert_params=expert_params,
            expert_quantization=expert_quantization,
        )
    shape = ModelShape(
        num_hidden_layers=int(stored["num_hidden_layers"]),
        num_key_value_heads=stored.get("num_key_value_heads"),
        head_dim=stored.get("head_dim"),
        kv_lora_rank=stored.get("kv_lora_rank"),
        qk_rope_head_dim=stored.get("qk_rope_head_dim"),
        context_length=stored.get("context_length"),
        num_attention_heads=stored.get("num_attention_heads"),
        hidden_size=stored.get("hidden_size"),
        sliding_window=stored.get("sliding_window"),
        sliding_layers=stored.get("sliding_layers") or 0,
    )
    return Sizing(
        total_params=parameter_count,
        shape=shape,
        quantization=quantization,
        expert_params=expert_params,
        expert_quantization=expert_quantization,
    )
