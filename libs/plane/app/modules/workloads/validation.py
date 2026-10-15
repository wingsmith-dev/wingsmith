"""Checks a workload's declared configuration before anything is written or placed.

Every `check_*` function here raises `Invalid` on a configuration that cannot be placed,
run or believed, and returns nothing otherwise. They read the request and the artifact and
nothing else, so create, update, reshape and promotion all refuse the same things.
"""

from __future__ import annotations

import math

from dataclasses import replace
from collections.abc import Sequence

import msgspec

from app.core import devices
from app.core.vram import Sizing
from app.core.vram import sizing_of
from app.core.vram import impossible
from app.db.models import Artifact
from app.db.models import WorkloadRole
from app.core.serving import TEXT
from app.core.serving import AGGREGATED
from app.core.serving import OMNI_ENGINES
from app.core.serving import DERIVED_FLAGS
from app.core.serving import SERVING_ROLES
from app.core.serving import TOPOLOGY_FLAGS
from app.core.serving import OUTPUT_MODALITIES
from app.core.serving import AMD64_ONLY_ENGINES
from app.core.serving import SERVING_TOPOLOGIES
from app.core.serving import is_omni
from app.core.serving import flag_name
from app.core.serving import check_retention
from app.core.serving import check_overlay_retention
from app.core.exceptions import Invalid

from .schemas import RouterSchema
from .schemas import ServingSchema
from .schemas import ReasoningSchema
from .schemas import CapabilitiesSchema
from .schemas import WorkloadServingRequest
from .serving import MODALITY_LIMITS
from .serving import declared_roles


DTYPE_VALUES = ("fp16", "float16", "bf16", "bfloat16", "fp32", "float32", "auto")
"""Precisions a checkpoint is stored in. None of them is a quantization method."""


def check_quantization(value: str | None) -> None:
    """Rejects a dtype put in the quantization field.

    The two are one keystroke apart in a form and mean opposite things: dtype is
    the precision weights are already in, quantization is a scheme for making them
    smaller than that. An engine takes only real methods on `--quantization`, so a
    dtype there is not a degraded serve — it is a worker that exits before it binds
    a port, and the cluster reports it as a crash loop with no mention of this
    field. Cheap to catch when someone writes it, expensive to diagnose an hour
    later from the other side of the wire.

    Unquantized weights declare nothing here. Empty is the correct answer for the
    majority of repos, which is why this rejects rather than translates: silently
    moving the value to `dtype` would guess at an intent the caller did not state.
    """
    if value is not None and value.strip().lower() in DTYPE_VALUES:
        raise Invalid(
            f"{value} is a dtype, not a quantization method; set dtype instead, "
            "and leave quantization empty for weights that ship unquantized",
            {"quantization": value},
        )


def check_footprint(**fields: int | None) -> None:
    """Rejects a negative resource request.

    Separate from the shape check because these are per-replica sizing hints
    rather than placement constraints: none of them decide which devices a workload
    can run on, and none of them can be below zero either.
    """
    for name, value in fields.items():
        if value is not None and value < 0:
            raise Invalid(f"{name} cannot be negative", {name: value})


def check_floor(device_class: str, device_memory_mib_min: int) -> None:
    """Rejects a per-device memory floor the declared device class cannot meet.

    A device class *is* a memory figure — `h100-80gb-sxm` and `h100-80gb-pcie` are
    separate entries of the table for exactly this reason — so a class and a floor
    are two statements about the same quantity and they can contradict each other.
    When they do, placement filters every device of that class out before it looks
    at whether any are free, reports `no_such_device_class`, and the workload
    expands to nothing: a cluster with eight idle accelerators of precisely the
    right kind reads as a cluster that owns none.

    That is not a capacity question and it must not be discovered by asking a
    scheduler. Both numbers are on the request and the table settles it, so the
    contradiction is refused where it is written.

    Silent about a class the catalogue does not name, like every other check here:
    an unmeasured card is not contradicted by arithmetic nobody can do.
    """
    if device_memory_mib_min <= 0:
        return
    device = devices.table().get(device_class)
    if device is None or device.memory_mib <= 0:
        return
    if device_memory_mib_min > device.memory_mib:
        raise Invalid(
            f"a floor of {device_memory_mib_min} MiB per device excludes every {device_class}, "
            f"which has {device.memory_mib} MiB",
            {
                "device_class": device_class,
                "device_memory_mib_min": device_memory_mib_min,
                "device_memory_mib": device.memory_mib,
            },
        )


def check_shape(
    device_class: str,
    device_count: int,
    tensor_parallel: int,
    pipeline_parallel: int,
    expert_parallel: int,
    context_length: int | None,
    kv_cache_fraction: float | None,
) -> None:
    """Rejects a shape that cannot be placed.

    `context_length` is None where the engine has no such thing — a diffusion model on omni —
    and is then not checked.

    Two rules, both from heterogeneity being a scheduling constraint rather than
    an enrolment detail. A shape without a device class is not a shape — you
    cannot tensor-parallel across an A100 and an H100 — and a device count that
    is not a whole multiple of tensor × pipeline parallelism describes a layout
    no scheduler can lay down.
    """
    if not device_class or not device_class.strip():
        raise Invalid("a shape without a device class is not a shape")
    for name, value in (
        ("device_count", device_count),
        ("tensor_parallel", tensor_parallel),
        ("pipeline_parallel", pipeline_parallel),
        ("expert_parallel", expert_parallel),
        ("context_length", context_length),
    ):
        if value is not None and value < 1:
            raise Invalid(f"{name} must be at least 1", {name: value})
    span = tensor_parallel * pipeline_parallel
    if device_count % span:
        raise Invalid(
            "device_count must be a whole multiple of tensor × pipeline parallelism",
            {"device_count": device_count, "tensor_parallel": tensor_parallel, "pipeline_parallel": pipeline_parallel},
        )
    if expert_parallel > device_count:
        raise Invalid(
            "expert_parallel exceeds the device count",
            {"expert_parallel": expert_parallel, "device_count": device_count},
        )
    if kv_cache_fraction is not None and not 0 < kv_cache_fraction <= 1:
        raise Invalid("kv_cache_fraction must be within (0, 1]", {"kv_cache_fraction": kv_cache_fraction})


def sizing_for(artifact: Artifact, quantization: str | None) -> Sizing | None:
    """The artifact's sizing facts, at the precision this workload means to serve it.

    The workload's quantization wins wherever it declares one. Serving fp16 weights
    at fp8 halves what has to be resident, so sizing against the repo's native
    precision would refuse shapes that work — the workload is the thing that decides
    how the weights are loaded, and the artifact only says how they ship.
    """
    sizing = sizing_of(artifact.metadata_, artifact.parameter_count)
    if sizing is None or not quantization:
        return sizing
    return replace(sizing, quantization=quantization)


def check_capacity(sizing: Sizing | None, device_class: str, device_count: int, tensor_parallel: int) -> None:
    """Rejects a shape whose devices cannot physically hold the weights.

    Separate from `check_shape` because it needs the artifact, and `check_shape`
    runs on arithmetic alone before one has been loaded. Separate from a full fit
    because of where the confidence runs out: `app.core.vram.impossible` is the half
    of a fit with no fudge factor in it, and whether a context window and a batch
    also fit is an estimate that belongs in a fitment view as advice. Enforcing the
    estimate would eventually refuse a workload that serves perfectly well.

    Silent about anything it cannot measure. An artifact whose repo published no
    attention shape or parameter count, and a device class the catalogue does not
    name, both pass — the plane does not refuse what it has no number for.

    `tensor_parallel` is the workload's own, not the device count. A replica of sixteen
    devices at tensor-eight by pipeline-two divides its heads by eight, and checking
    against sixteen would refuse a layout that works.
    """
    if sizing is None:
        return
    device = devices.table().get(device_class)
    if device is None:
        return
    reason = impossible(sizing, device, device_count, tensor_parallel=tensor_parallel)
    if reason is not None:
        raise Invalid(
            f"this shape cannot serve the artifact: {reason}",
            {
                "device_class": device_class,
                "device_count": device_count,
                "tensor_parallel": tensor_parallel,
                "quantization": sizing.quantization,
            },
        )


DISAGGREGATION_INCAPABLE_ENGINES: frozenset[str] = OMNI_ENGINES
"""Engines the plane knows have no prefill/decode split, and refuses one for.

A deny list rather than an allow list because this module refuses only what it can prove:
`check_capacity` states the governing habit — "the plane does not refuse what it has no number
for" — and `resolve_engine_runtime_image` returns `None` rather than inventing an address for an
engine nobody configured. An allow list would refuse every engine added after this line was
written, which is the failure mode of a closed set that is not actually closed.

The omni engine is the member: its pipeline has stages rather than a prefill/decode pair,
and the operator renders it as one aggregated `worker`.
"""


def check_topology(serving_topology: str, roles: Sequence[WorkloadRole], engine: str) -> None:
    """Rejects a disaggregation declaration that cannot be rendered, run, or believed.

    Here beside `check_shape` and `check_capacity` for the reason those two are here: the person
    can still fix it. A topology mistake that surfaces as a failed reconcile is a mistake
    discovered by a model that did not start, twenty minutes of weight loading later.

    What is deliberately **not** refused is as load-bearing as what is.

    Weights are free positive integers, not coprime. Coprimality was proposed so that each ratio
    would have exactly one spelling, but the argument rested on the weights being part of the
    configuration's identity and they are not — `serving_digest` excludes them. What it costs is
    real: it makes "two of each" inexpressible as `2:2`, forcing it to be written `1:1` at two
    groups, which reinterprets a live scale number to say the same thing.

    Per-role capacity is not checked separately, because there is nothing separate to check. The
    two roles share `device_class`, `device_count` and `tensor_parallel` — the recipe's whole
    delta is six arguments — so `check_capacity` on the workload already answers for both. A
    second call with identical inputs would be a check that cannot fail differently.

    Whether a group is expressible in whole nodes is not checked either. It cannot be:
    `devices.table()` carries per-device memory and not node shape, so devices-per-node is a fact
    about a cluster rather than about a device class. `_tensor_group_too_wide` already asks it at
    placement, where the cluster is in hand, and a weaker copy here would refuse workloads that
    some cluster in the fleet can serve.
    """
    if serving_topology not in SERVING_TOPOLOGIES:
        raise Invalid(
            f"serving_topology must be one of {sorted(SERVING_TOPOLOGIES)}",
            {"serving_topology": serving_topology},
        )
    if serving_topology == AGGREGATED:
        if roles:
            raise Invalid(
                "an aggregated workload has one worker and no roles: "
                "declaring a ratio it will not serve at is worse than refusing it",
                {"serving_topology": serving_topology, "roles": [role.role for role in roles]},
            )
        return
    if (engine or "").strip().lower() in DISAGGREGATION_INCAPABLE_ENGINES:
        raise Invalid(f"the {engine} engine has no prefill/decode split", {"engine": engine})

    declared = [role.role for role in roles]
    if sorted(declared) != sorted(SERVING_ROLES):
        raise Invalid(
            f"a disaggregated workload is exactly the roles {sorted(SERVING_ROLES)}; one of them is not a topology",
            {"roles": declared},
        )
    for role in roles:
        if role.weight is None or role.weight < 1:
            raise Invalid(
                f"the {role.role} weight must be at least 1: a role with no workers is not a role",
                {"role": role.role, "weight": role.weight},
            )
        for key in role.engine_args or {}:
            flag = flag_name(str(key))
            if flag in DERIVED_FLAGS or flag in TOPOLOGY_FLAGS:
                raise Invalid(
                    f"the {role.role} role may not set --{flag}: it is rendered from a declared column "
                    "or from the topology, so a value here is not a default but a contradiction "
                    "of a decision already acted on",
                    {"role": role.role, "flag": flag},
                )


TRANSPORTS = frozenset({"reasoning_content", "inline_tags"})


STREAM_USAGE = frozenset({"final_chunk", "every_chunk", "on_request"})


CONTEXT_OVERFLOW = frozenset({"reject", "truncate_input", "clamp_output"})


STRUCTURED_OUTPUT_MODES = frozenset({"text", "json_object", "json_schema"})


TOOL_CHOICE_MODES = frozenset({"auto", "none", "required", "named", "allowed_tools"})


INPUT_MODALITIES = frozenset({"text", "image", "audio", "video"})


PROMPT_CACHE_MODES = frozenset({"off", "automatic", "explicit"})


def _members(name: str, values: list[str], allowed: frozenset[str]) -> None:
    unknown = [v for v in values if v not in allowed]
    if unknown:
        raise Invalid(f"unknown {name}: {', '.join(sorted(unknown))}", {name: values, "allowed": sorted(allowed)})
    if len(set(values)) != len(values):
        raise Invalid(f"{name} is a set and repeats a member", {name: values})


def _reasoning_declared(reasoning: ReasoningSchema) -> bool:
    """Whether anything about reasoning was declared rather than left at its default."""
    return bool(
        reasoning.default_on
        or reasoning.parser
        or reasoning.transport == "inline_tags"
        or reasoning.think_begin_token
        or reasoning.think_end_token
        or reasoning.suppress_eos_until_think_end
    )


ROUTER_MODES = frozenset({"kv", "round-robin", "random", "power-of-two", "least-loaded", "device-aware-weighted"})
"""The `--router-mode` choices of Dynamo 1.5.0's frontend, less `direct`.

`direct` routes to a worker id the request itself carries. Nothing in front of a Hangar frontend sets
one, so a frontend in that mode refuses every request the gateway forwards to it.
"""


ROUTER_QUEUE_POLICIES = frozenset({"fcfs", "wspt"})
"""The `--router-queue-policy` choices of Dynamo 1.5.0's frontend.

The Rust config also parses `lcfs`, but the flag the frontend is started through does not offer it,
so a value only the lower layer accepts is not one to declare.
"""


KV_ROUTER_SETTINGS = ("queue_threshold", "queue_policy", "kv_overlap_score_credit")
"""The settings only the KV router reads. Dynamo builds its KV configuration for `kv` mode alone."""


def check_router(router: RouterSchema) -> None:
    """Rejects a router block Dynamo would refuse, or one that would silently do nothing.

    The ranges are Dynamo's own: `kv-router` validates `router_queue_threshold` as `>= 0` and
    `overlap_score_credit` as finite and non-negative, and the frontend fails at startup on
    anything else — a crash-looping frontend rather than an error on the declaration.

    The two consistency rules are this plane's, and they are the `structured_output_backend` rule
    again: a setting that cannot take effect is refused rather than stored, because storing it lets
    somebody believe they asked for something. The router only queues once a threshold is set, so a
    policy without one orders an empty queue; and a KV setting beside another mode is never read.
    """
    if router.mode is not None and router.mode not in ROUTER_MODES:
        raise Invalid(
            f"unknown router mode {router.mode!r}",
            {"mode": router.mode, "allowed": sorted(ROUTER_MODES)},
        )
    if router.queue_policy is not None and router.queue_policy not in ROUTER_QUEUE_POLICIES:
        raise Invalid(
            f"unknown router queue_policy {router.queue_policy!r}",
            {"queue_policy": router.queue_policy, "allowed": sorted(ROUTER_QUEUE_POLICIES)},
        )
    for field_name in ("queue_threshold", "kv_overlap_score_credit"):
        value = getattr(router, field_name)
        if value is not None and not (math.isfinite(value) and value >= 0):
            raise Invalid(f"router {field_name} must be a finite number >= 0", {field_name: value})
    if router.queue_policy is not None and router.queue_threshold is None:
        raise Invalid(
            "a router queue_policy is meaningless without a queue_threshold, because the router only queues once one is set",
            {"queue_policy": router.queue_policy, "queue_threshold": None},
        )
    kv_only = {name: getattr(router, name) for name in KV_ROUTER_SETTINGS if getattr(router, name) is not None}
    if kv_only and router.mode not in (None, "kv"):
        raise Invalid(
            f"{', '.join(sorted(kv_only))} only apply to the kv router, and mode is {router.mode!r}",
            {"mode": router.mode, **kv_only},
        )


def check_serving(
    serving: ServingSchema,
    capabilities: CapabilitiesSchema,
    prefix_cache_enabled: bool,
) -> None:
    """Rejects a serving configuration that contradicts itself.

    Every rule here is a consistency check between two things the workload declares
    — never a check that a capability claim is true. The plane holds no table of
    what an engine can do and is not being given one: a block that is internally
    consistent and entirely false is accepted, and a conformance run is the only
    thing that can falsify it.
    """
    reasoning = serving.reasoning
    if reasoning.transport not in TRANSPORTS:
        raise Invalid(
            f"unknown reasoning transport {reasoning.transport!r}",
            {"transport": reasoning.transport, "allowed": sorted(TRANSPORTS)},
        )
    if serving.stream_usage not in STREAM_USAGE:
        raise Invalid(
            f"unknown stream_usage {serving.stream_usage!r}",
            {"stream_usage": serving.stream_usage, "allowed": sorted(STREAM_USAGE)},
        )
    if serving.context_overflow not in CONTEXT_OVERFLOW:
        raise Invalid(
            f"unknown context_overflow {serving.context_overflow!r}",
            {"context_overflow": serving.context_overflow, "allowed": sorted(CONTEXT_OVERFLOW)},
        )

    if reasoning.transport == "inline_tags" and not (reasoning.think_begin_token and reasoning.think_end_token):
        raise Invalid(
            "inline_tags needs both think tokens, because a client parsing the trace needs the literals",
            {
                "think_begin_token": reasoning.think_begin_token,
                "think_end_token": reasoning.think_end_token,
            },
        )
    if reasoning.suppress_eos_until_think_end and not reasoning.think_end_token:
        raise Invalid(
            "suppress_eos_until_think_end needs the token it suppresses until",
            {"think_end_token": reasoning.think_end_token},
        )
    if _reasoning_declared(reasoning) and not capabilities.reasoning_supported:
        raise Invalid(
            "a workload that is not claimed to reason cannot declare how it reasons",
            {"reasoning": msgspec.to_builtins(reasoning), "reasoning_supported": False},
        )

    _members("structured_output_modes", capabilities.structured_output_modes, STRUCTURED_OUTPUT_MODES)
    _members("tool_choice_modes", capabilities.tool_choice_modes, TOOL_CHOICE_MODES)
    _members("input_modalities", capabilities.input_modalities, INPUT_MODALITIES)

    if serving.structured_output_backend and "json_schema" not in capabilities.structured_output_modes:
        raise Invalid(
            "a structured output backend is meaningless without json_schema among the modes",
            {
                "structured_output_backend": serving.structured_output_backend,
                "structured_output_modes": capabilities.structured_output_modes,
            },
        )
    if bool(capabilities.tool_choice_modes) != capabilities.tool_calling_supported:
        raise Invalid(
            "tool_choice_modes is non-empty exactly when tool calling is supported",
            {
                "tool_choice_modes": capabilities.tool_choice_modes,
                "tool_calling_supported": capabilities.tool_calling_supported,
            },
        )
    if serving.tool_call_parser and not capabilities.tool_calling_supported:
        raise Invalid(
            "a tool call parser is meaningless on a workload that does not claim tool calling",
            {"tool_call_parser": serving.tool_call_parser},
        )

    if not capabilities.input_modalities:
        raise Invalid(
            "a workload takes at least one input modality",
            {"input_modalities": capabilities.input_modalities},
        )
    for field_name, modality in MODALITY_LIMITS.items():
        limit = getattr(serving, field_name)
        declared = modality in capabilities.input_modalities
        if limit is None:
            if declared:
                raise Invalid(
                    f"a workload claiming {modality} must set {field_name}, "
                    f"because an unset cap renders as zero and the engine refuses the modality",
                    {field_name: None, "input_modalities": capabilities.input_modalities},
                )
            continue
        if not declared:
            raise Invalid(
                f"a {modality} limit is meaningless without {modality} among the input modalities",
                {field_name: limit, "input_modalities": capabilities.input_modalities},
            )
        if limit < 1:
            raise Invalid(f"{field_name} must be at least 1", {field_name: limit})

    if capabilities.prompt_cache_mode not in PROMPT_CACHE_MODES:
        raise Invalid(
            f"unknown prompt_cache_mode {capabilities.prompt_cache_mode!r}",
            {"prompt_cache_mode": capabilities.prompt_cache_mode, "allowed": sorted(PROMPT_CACHE_MODES)},
        )
    if capabilities.prompt_cache_mode != "off" and not prefix_cache_enabled:
        raise Invalid(
            "a prompt cache contract needs the mechanism under it",
            {"prompt_cache_mode": capabilities.prompt_cache_mode, "prefix_cache_enabled": prefix_cache_enabled},
        )
    if capabilities.prompt_cache_ttl_seconds is not None and capabilities.prompt_cache_ttl_seconds < 0:
        raise Invalid(
            "prompt_cache_ttl_seconds cannot be negative",
            {"prompt_cache_ttl_seconds": capabilities.prompt_cache_ttl_seconds},
        )


def check_output(engine: str, serving: ServingSchema) -> None:
    """Rejects an output modality the engine cannot emit, or a diffusion block it cannot read.

    The one place the plane knows something about an engine, and it knows it because the
    command line differs: only `dynamo.vllm.omni` takes `--output-modalities` and a stage-config
    file, and omni exits without the former. So an omni workload must say `image` or `video`, and
    every other engine emits text and may not claim otherwise — a chat worker handed an image
    modality would either crash on the flag or come up healthy answering the wrong API.
    """
    modality = serving.output_modality
    if modality is not None and modality not in OUTPUT_MODALITIES:
        raise Invalid(
            f"unknown output_modality {modality!r}",
            {"output_modality": modality, "allowed": sorted(OUTPUT_MODALITIES)},
        )
    if is_omni(engine):
        if modality in (None, TEXT):
            raise Invalid(
                "the omni engine needs output_modality image or video: it is what "
                "--output-modalities renders from, and the worker will not start without it",
                {"engine": engine, "output_modality": modality},
            )
        diffusion = serving.diffusion
        if diffusion is not None and diffusion.model_class_name is not None and not diffusion.model_class_name.strip():
            raise Invalid(
                "diffusion.model_class_name cannot be blank", {"model_class_name": diffusion.model_class_name}
            )
        return
    if modality not in (None, TEXT):
        raise Invalid(
            f"the {engine} engine emits text; image and video are the omni engine's",
            {"engine": engine, "output_modality": modality},
        )
    if serving.diffusion is not None:
        raise Invalid(
            f"a diffusion block is read only by the omni engine, not {engine}",
            {"engine": engine},
        )


def check_host_architecture(engine: str, device_class: str) -> None:
    """Rejects an engine on a device class whose host CPU it has no build for.

    Placement matches a workload's device class exactly, so refusing the pair here keeps it off
    every cluster — named ones included — before anything is admitted. The alternative is a
    workload placed on a GB200 and an amd64 image pulled onto an arm64 node, which fails in a
    pod log rather than on the declaration. A class the table does not carry is not refused:
    the plane does not refuse what it has no fact for.
    """
    if (engine or "").strip().lower() not in AMD64_ONLY_ENGINES:
        return
    device = devices.table().get(device_class)
    if device is not None and device.host_architecture not in ("", "amd64"):
        raise Invalid(
            f"the {engine} engine is built for amd64 hosts only and {device_class} is attached to "
            f"{device.host_architecture}",
            {"engine": engine, "device_class": device_class, "host_architecture": device.host_architecture},
        )


def check_serving_request(data: WorkloadServingRequest, artifact: Artifact) -> None:
    """Rejects a serving configuration that cannot be placed, run, or believed.

    One entry point for create and update alike: an update is validated as the
    whole configuration it would leave behind, never as the fields it names, so a
    patch cannot arrive at a state a create would have refused.
    """
    check_shape(
        device_class=data.shape.device_class,
        device_count=data.shape.device_count,
        tensor_parallel=data.parallelism.tensor,
        pipeline_parallel=data.parallelism.pipeline,
        expert_parallel=data.parallelism.expert,
        context_length=None if is_omni(data.engine) else data.context_length,
        kv_cache_fraction=data.cache.kv_cache_fraction,
    )
    check_footprint(
        device_memory_mib_min=data.shape.device_memory_mib_min,
        max_batch_tokens=data.max_batch_tokens,
        cpu_cores=data.resources.cpu_cores,
        memory_mib=data.resources.memory_mib,
        disk_gib=data.resources.disk_gib,
    )
    check_serving(data.serving, data.capabilities, data.cache.prefix_cache_enabled)
    check_output(data.engine, data.serving)
    check_host_architecture(data.engine, data.shape.device_class)
    check_router(data.router)
    check_quantization(data.quantization)
    check_floor(data.shape.device_class, data.shape.device_memory_mib_min)
    check_capacity(
        sizing_for(artifact, data.quantization),
        data.shape.device_class,
        data.shape.device_count,
        data.parallelism.tensor,
    )
    check_topology(data.topology.serving_topology, declared_roles(data.topology), data.engine)
    check_retention(data.engine_args)
    for role in declared_roles(data.topology):
        check_retention(role.engine_args, f"the {role.role} role")
    check_overlay_retention(msgspec.to_builtins(data.overlay))


def check_scale(min_scale: int, desired_scale: int, max_scale: int, kind: str, gang_size: int) -> None:
    """Rejects totals that contradict each other, or a gang that pretends to a range.

    Zero is a legitimate floor — a workload scaled to nothing is parked, not
    deleted — so for a splittable kind the only rules are non-negativity and the
    ordering of the three numbers.

    A gang is different in kind and not just in degree: its ranks are co-scheduled,
    so `gang_size` of them either run together or none do. A range would say the
    plane may run part of a gang, which is not a smaller gang but a broken one.
    """
    for name, value in (("min_scale", min_scale), ("desired_scale", desired_scale), ("max_scale", max_scale)):
        if value < 0:
            raise Invalid(f"{name} cannot be negative", {name: value})
    if max_scale < min_scale:
        raise Invalid("max_scale is below min_scale", {"min_scale": min_scale, "max_scale": max_scale})
    if not min_scale <= desired_scale <= max_scale:
        raise Invalid(
            "desired_scale is outside the declared bounds",
            {"min_scale": min_scale, "desired_scale": desired_scale, "max_scale": max_scale},
        )
    if kind == "gang" and not (min_scale == desired_scale == max_scale == gang_size):
        raise Invalid(
            "a gang has no scale range: its ranks are co-scheduled, so the three totals must all equal gang_size",
            {
                "gang_size": gang_size,
                "min_scale": min_scale,
                "desired_scale": desired_scale,
                "max_scale": max_scale,
            },
        )
