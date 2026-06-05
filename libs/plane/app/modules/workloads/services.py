from __future__ import annotations

import datetime as dt

from uuid import UUID
from decimal import Decimal
from dataclasses import replace

import msgspec

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import devices
from app.config import settings
from app.core.ids import POLICY
from app.core.ids import WORKLOAD
from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.core.vram import Sizing
from app.core.vram import sizing_of
from app.core.vram import impossible
from app.db.models import Policy
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import PriorityClass
from app.db.models import PolicyAttachment
from app.db.notify import DEPLOYMENT as DEPLOYMENT_KIND
from app.db.notify import Notifier
from app.db.notify import DeclaredChange
from app.db.notify import DatabaseNotifier
from app.ingestion import statements
from app.core.schemas import ListResponse
from app.core.serving import served_model
from app.core.serving import serving_digest
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.placement.services import PlacementService
from app.modules.placement.snapshot import PolicySnapshot
from app.modules.placement.snapshot import CapacitySnapshot
from app.modules.deployments.schemas import DeploymentStatusSchema
from app.modules.deployments.schemas import DeploymentReplicaRollupSchema
from app.modules.placement.repository import SUBJECT_WORKLOAD

from .schemas import CacheSchema
from .schemas import ShapeSchema
from .schemas import OverlaySchema
from .schemas import ServingSchema
from .schemas import WorkloadSchema
from .schemas import ReasoningSchema
from .schemas import ResourcesSchema
from .schemas import ParallelismSchema
from .schemas import CapabilitiesSchema
from .schemas import WorkloadScaleSchema
from .schemas import WorkloadCreateRequest
from .schemas import WorkloadServingSchema
from .schemas import WorkloadServingUpdate
from .schemas import WorkloadUpdateRequest
from .schemas import WorkloadServingRequest
from .schemas import WorkloadPlacementSchema
from .schemas import WorkloadDeploymentSchema
from .schemas import WorkloadPlacementNodeSchema
from .schemas import WorkloadReplicaPlacementSchema
from .repository import WorkloadRepository


KINDS = frozenset({"service", "task", "gang"})
STRATEGIES = frozenset({"pack", "spread"})
CONSTRAINT_SUFFIX = "-placement"
PHASES = frozenset({"pending", "admitted", "placed", "running", "succeeded", "failed"})

MANAGED = "managed"

UNITS = {"service": "replicas", "gang": "ranks", "task": "completions"}
"""What the three scale numbers count, named by kind.

The numbers are the same integers whatever the kind, and they mean three
different things; a console that renders `8` without saying whether that is eight
replicas or eight ranks is unreadable, so the unit travels with them.
"""

PHASE_ORDER = ("pending", "admitted", "placed", "running", "succeeded")
"""Admission phases from least to most advanced, for the workload rollup.

`failed` is deliberately absent: it is not a point on this line. One failed
deployment makes the workload failed regardless of how far the others got, which
is a different rule from "the least advanced wins" and is applied before it.
"""

EMPTY = DeploymentReplicaRollupSchema(
    total=0,
    ready=0,
    running=0,
    pending=0,
    failed=0,
    at_current_generation=0,
)


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


def check_shape(
    device_class: str,
    device_count: int,
    tensor_parallel: int,
    pipeline_parallel: int,
    expert_parallel: int,
    context_length: int,
    kv_cache_fraction: float | None,
) -> None:
    """Rejects a shape that cannot be placed.

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
        if value < 1:
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


TRANSPORTS = frozenset({"reasoning_content", "inline_tags"})
STREAM_USAGE = frozenset({"final_chunk", "every_chunk", "on_request"})
CONTEXT_OVERFLOW = frozenset({"reject", "truncate_input", "clamp_output"})
STRUCTURED_OUTPUT_MODES = frozenset({"text", "json_object", "json_schema"})
TOOL_CHOICE_MODES = frozenset({"auto", "none", "required", "named"})
INPUT_MODALITIES = frozenset({"text", "image", "audio", "video"})
PROMPT_CACHE_MODES = frozenset({"off", "automatic", "explicit"})

SERVING_FLAGS: dict[str, dict[str, str]] = {
    "vllm": {
        "parser": "reasoning-parser",
        "tool_call_parser": "tool-call-parser",
        "structured_output_backend": "guided-decoding-backend",
        "max_images_per_request": "limit-mm-per-prompt",
    },
    "sglang": {
        "parser": "reasoning-parser",
        "tool_call_parser": "tool-call-parser",
        "structured_output_backend": "grammar-backend",
    },
}
"""The engine argument each typed serving field speaks for, per engine.

Keyed without the leading dashes, the way the operator keys them once it has
trimmed them. Only the fields that genuinely map to one flag are here: an engine
this plane has never heard of maps nothing, and neither `context_overflow` nor
EOS suppression is a single flag on every engine — those are the operator's to
render, and it is entitled to fail a reconcile rather than drop one silently."""


SERVING_IMPLIED_FLAGS: dict[str, dict[str, tuple[str, ...]]] = {
    "vllm": {
        "tool_call_parser": ("enable-auto-tool-choice",),
    },
}
"""Valueless flags a typed field cannot live without, per engine.

vLLM parses tool calls with `--tool-call-parser` and *dispatches* them only with
`--enable-auto-tool-choice`; set the first alone and the engine loads a parser it
never runs, so the workload reads as configured and the endpoint returns the call as
prose in `content`. Every one of the fifteen models in the vLLM recipe corpus that
declares tool calling ships the two flags together, and none ships either alone.

They are here rather than as a second typed field because they are not a second
decision. A serving block able to express one without the other can only express a
broken state, and the field that renders them already carries the intent."""


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

    if "text" not in capabilities.input_modalities:
        raise Invalid("text is always an input modality", {"input_modalities": capabilities.input_modalities})
    if serving.max_images_per_request is not None:
        if "image" not in capabilities.input_modalities:
            raise Invalid(
                "an image limit is meaningless without image among the input modalities",
                {"input_modalities": capabilities.input_modalities},
            )
        if serving.max_images_per_request < 1:
            raise Invalid(
                "max_images_per_request must be at least 1",
                {"max_images_per_request": serving.max_images_per_request},
            )

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


def serving_args(engine: str, serving: ServingSchema) -> dict[str, str]:
    """The engine arguments the typed serving block speaks for, keyed without dashes."""
    flags = SERVING_FLAGS.get(engine.strip().lower(), {})
    args: dict[str, str] = {}
    for field_name, value in (
        ("parser", serving.reasoning.parser),
        ("tool_call_parser", serving.tool_call_parser),
        ("structured_output_backend", serving.structured_output_backend),
    ):
        if value and field_name in flags:
            args[flags[field_name]] = value
    if serving.max_images_per_request is not None and "max_images_per_request" in flags:
        args[flags["max_images_per_request"]] = f"image={serving.max_images_per_request}"
    return args


def serving_companion_args(engine: str, serving: ServingSchema) -> dict[str, str]:
    """The valueless flags a typed field cannot work without, keyed without dashes.

    Separate from `serving_args` because the precedence is the other way round. A
    typed field beats `engine_args`, since it is the value the plane can validate and
    display. A companion flag does not: it is inferred rather than declared, so a
    workload that spells it out by hand — for an engine build that wants a value there,
    or to hold a flag this table gets wrong — keeps what it wrote.
    """
    implied = SERVING_IMPLIED_FLAGS.get(engine.strip().lower(), {})
    args: dict[str, str] = {}
    if serving.tool_call_parser:
        for flag in implied.get("tool_call_parser", ()):
            args[flag] = ""
    return args


def engine_runtime_repos() -> dict[str, str]:
    """`ENGINE_RUNTIME_REPOS` parsed into `engine -> repository`.

    Malformed pairs are dropped rather than raised on: this is read on the write
    path of every workload, and one bad character in an environment variable must
    not be the reason a workload cannot be created.
    """
    repos: dict[str, str] = {}
    for pair in settings.ENGINE_RUNTIME_REPOS.split(","):
        engine, _, repository = pair.strip().partition("=")
        if engine and repository:
            repos[engine.strip().lower()] = repository.strip()
    return repos


def resolve_engine_runtime_image(engine: str, engine_runtime_version: str, declared: str | None) -> str | None:
    """The image a workload serves with, resolved from its engine and runtime version.

    The engine picks the repository and the runtime version is the tag, because a
    Dynamo runtime image is exactly those two things: `vllm-runtime` is the build
    with vLLM and `dynamo.vllm` in it, and `1.4.1` is which Dynamo release. The
    engine's own version appears nowhere and cannot — which vLLM sits inside
    `vllm-runtime:1.4.1` is NVIDIA's business and is not on any wire here.

    A declared image is returned untouched — pinning an exact image is the whole
    reason the field is writable, and a workload that names one has already said
    what it means. Everything else is the common case: an author picks `vllm` and
    a runtime version, and the repository those two imply is configuration, not a
    thing to retype.

    Returns `None` for an engine with no configured repository, which leaves the
    workload exactly as it was before this existed. That is deliberate: inventing an
    address for an engine the plane has never been told about would trade a
    visible "no image" for an `ImagePullBackOff` on a cluster.
    """
    if declared:
        return declared
    repository = engine_runtime_repos().get((engine or "").strip().lower())
    if not repository:
        return None
    tag = (engine_runtime_version or "").strip()
    if not tag or tag == "latest":
        tag = settings.ENGINE_RUNTIME_TAG_DEFAULT
    return f"{repository}:{tag}"


def resolved_engine_args(engine: str, engine_args: dict | None, serving: ServingSchema) -> dict[str, str]:
    """`engine_args` with the typed serving flags laid over the top.

    The typed field wins the collision. `engine_args` stays the escape hatch for a
    flag nothing here models, but a flag that *is* modelled has one declared home,
    and a workload that pins `--reasoning-parser` by hand while also naming
    `serving.reasoning.parser` is stating the same thing twice — the typed value is
    the one the plane can validate, display and verify, so it is the one that
    stands.

    Derived on every read and stored nowhere: `engine_args` is left exactly as it
    was written.

    TODO: the operator renders the other way round today — `engineArgs`
    (`libs/operator/internal/render/dynamo.go:319`) lays `engine_args` over its
    derived flags last, so whichever way the serving block eventually reaches the
    wire, the two have to be made to agree or this becomes a resolved view of
    something the cluster does not do.
    """
    resolved = {str(key).removeprefix("--"): str(value) for key, value in (engine_args or {}).items()}
    for flag, value in serving_companion_args(engine, serving).items():
        resolved.setdefault(flag, value)
    resolved.update(serving_args(engine, serving))
    return resolved


def serving_block(stored: dict | None) -> ServingSchema:
    return msgspec.convert(stored or {}, ServingSchema)


def capabilities_block(stored: dict | None) -> CapabilitiesSchema:
    return msgspec.convert(stored or {}, CapabilitiesSchema)


FRACTION_SCALE = Decimal("0.0001")
"""The scale `demand_workload.kv_cache_fraction` is stored at, as `Numeric(5, 4)`.

Written through on the way in so that the value a create holds in memory is the
value Postgres hands back on the next read. The digest hashes this column as the
string it round-trips as, so a `0.9` that stayed a float until the next update
would hash differently from the `0.9000` the database returned — the same
configuration with two identities, produced by nothing but a reload.
"""


def _fraction(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(FRACTION_SCALE)


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
        context_length=data.context_length,
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
    check_quantization(data.quantization)
    check_capacity(
        sizing_for(artifact, data.quantization),
        data.shape.device_class,
        data.shape.device_count,
        data.parallelism.tensor,
    )


def serving_columns(data: WorkloadServingRequest, artifact: Artifact) -> dict:
    """A validated serving block as the columns it is stored in.

    The one place the nested request shape becomes flat columns, so create,
    promotion and update all write the same fields from the same reading of them.
    `serving_digest` is not here: it is derived from the row after these have
    landed on it, by `app.core.serving`, which is the only thing entitled to
    compute it.
    """
    return {
        "artifact_id": artifact.id,
        "engine": data.engine,
        "engine_runtime_version": data.engine_runtime_version,
        "engine_runtime_image": resolve_engine_runtime_image(
            data.engine, data.engine_runtime_version, data.engine_runtime_image
        ),
        "frontend_image": data.frontend_image,
        "engine_args": data.engine_args,
        "imported_args": data.imported_args,
        "quantization": data.quantization,
        "dtype": data.dtype,
        "tensor_parallel": data.parallelism.tensor,
        "pipeline_parallel": data.parallelism.pipeline,
        "expert_parallel": data.parallelism.expert,
        "context_length": data.context_length,
        "max_batch_tokens": data.max_batch_tokens,
        "kv_cache_dtype": data.cache.kv_cache_dtype,
        "kv_cache_fraction": _fraction(data.cache.kv_cache_fraction),
        "prefix_cache_enabled": data.cache.prefix_cache_enabled,
        "prefix_cache_config": data.cache.prefix_cache_config,
        "device_class": data.shape.device_class,
        "device_count": data.shape.device_count,
        "device_memory_mib_min": data.shape.device_memory_mib_min,
        "requires_single_topology_domain": data.shape.requires_single_topology_domain,
        "topology_domain_kind": data.shape.topology_domain_kind,
        "cpu_cores": data.resources.cpu_cores,
        "memory_mib": data.resources.memory_mib,
        "disk_gib": data.resources.disk_gib,
        "overlay": msgspec.to_builtins(data.overlay),
        "serving": msgspec.to_builtins(data.serving),
        "capabilities": msgspec.to_builtins(data.capabilities),
    }


def serving_request(workload: Workload, artifact: Artifact) -> WorkloadServingRequest:
    """A stored workload read back as the request that would have created it.

    A patch is applied to this and the result validated whole, which is what keeps
    one description of a legal configuration rather than one for creates and a
    weaker one for updates.
    """
    fraction = workload.kv_cache_fraction
    return WorkloadServingRequest(
        artifact=artifact.uid,
        engine=workload.engine,
        engine_runtime_version=workload.engine_runtime_version,
        engine_runtime_image=workload.engine_runtime_image,
        frontend_image=workload.frontend_image,
        engine_args=workload.engine_args,
        imported_args=workload.imported_args,
        quantization=workload.quantization,
        dtype=workload.dtype,
        parallelism=ParallelismSchema(
            tensor=workload.tensor_parallel,
            pipeline=workload.pipeline_parallel,
            expert=workload.expert_parallel,
        ),
        context_length=workload.context_length,
        max_batch_tokens=workload.max_batch_tokens,
        cache=CacheSchema(
            kv_cache_dtype=workload.kv_cache_dtype,
            kv_cache_fraction=None if fraction is None else float(fraction),
            prefix_cache_enabled=workload.prefix_cache_enabled,
            prefix_cache_config=workload.prefix_cache_config,
        ),
        shape=ShapeSchema(
            device_class=workload.device_class,
            device_count=workload.device_count,
            device_memory_mib_min=workload.device_memory_mib_min,
            requires_single_topology_domain=workload.requires_single_topology_domain,
            topology_domain_kind=workload.topology_domain_kind,
        ),
        resources=ResourcesSchema(
            cpu_cores=workload.cpu_cores,
            memory_mib=workload.memory_mib,
            disk_gib=workload.disk_gib,
        ),
        overlay=OverlaySchema(
            pod_spec=(workload.overlay or {}).get("pod_spec", {}) or {},
            dynamo=(workload.overlay or {}).get("dynamo", {}) or {},
        ),
        serving=serving_block(workload.serving),
        capabilities=capabilities_block(workload.capabilities),
    )


def serving_view(workload: Workload, artifact_uid: str) -> WorkloadServingSchema:
    """The serving block as a read returns it, with the resolved arguments beside it."""
    fraction = workload.kv_cache_fraction
    serving = serving_block(workload.serving)
    return WorkloadServingSchema(
        artifact_id=artifact_uid,
        engine=workload.engine,
        engine_runtime_version=workload.engine_runtime_version,
        engine_runtime_image=workload.engine_runtime_image,
        frontend_image=workload.frontend_image,
        engine_args=workload.engine_args,
        imported_args=workload.imported_args,
        quantization=workload.quantization,
        dtype=workload.dtype,
        parallelism=ParallelismSchema(
            tensor=workload.tensor_parallel,
            pipeline=workload.pipeline_parallel,
            expert=workload.expert_parallel,
        ),
        context_length=workload.context_length,
        max_batch_tokens=workload.max_batch_tokens,
        cache=CacheSchema(
            kv_cache_dtype=workload.kv_cache_dtype,
            kv_cache_fraction=None if fraction is None else float(fraction),
            prefix_cache_enabled=workload.prefix_cache_enabled,
            prefix_cache_config=workload.prefix_cache_config,
        ),
        serving=serving,
        capabilities=capabilities_block(workload.capabilities),
        resolved_engine_args=resolved_engine_args(workload.engine, workload.engine_args, serving),
        shape=ShapeSchema(
            device_class=workload.device_class,
            device_count=workload.device_count,
            device_memory_mib_min=workload.device_memory_mib_min,
            requires_single_topology_domain=workload.requires_single_topology_domain,
            topology_domain_kind=workload.topology_domain_kind,
        ),
        resources=ResourcesSchema(
            cpu_cores=workload.cpu_cores,
            memory_mib=workload.memory_mib,
            disk_gib=workload.disk_gib,
        ),
        overlay=OverlaySchema(
            pod_spec=(workload.overlay or {}).get("pod_spec", {}) or {},
            dynamo=(workload.overlay or {}).get("dynamo", {}) or {},
        ),
    )


def apply_serving(workload: Workload, data: WorkloadServingRequest, artifact: Artifact) -> None:
    """Writes a validated serving configuration onto a workload, digest included.

    The digest is computed from the row after the columns have landed on it rather
    than from the request, so what is hashed is what was stored — a coercion the
    write performs and the hash did not see would be a digest that names a
    configuration nobody is running.
    """
    for key, value in serving_columns(data, artifact).items():
        setattr(workload, key, value)
    workload.serving_digest = serving_digest(workload, artifact)


def constraint_name(workload_name: str) -> str:
    """The name of the policy a workload's own allow-list becomes.

    Derived from the workload rather than random so that the policies list reads as a
    list of intentions instead of a list of identifiers, and so a second workload of the
    same name in another project collides loudly — `Policy.name` is globally unique, and
    a silent second constraint sharing one name would be worse than the conflict.
    """
    return f"{workload_name}{CONSTRAINT_SUFFIX}"


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


def derive_phase(deployments: list[Deployment]) -> str:
    """What a workload's phase is, given what became of it in each cluster.

    Derived and never stored. A workload that is running in one region and queued
    on quota in another has no single stored phase that is true, and inventing one
    would make the two indistinguishable from a workload that is halfway
    everywhere.

    A workload with no deployments at all is `pending`: placement either has not
    run yet or found nowhere to put it, and both are states it may still leave.
    """
    if not deployments:
        return "pending"
    phases = [d.admission_phase for d in deployments]
    if "failed" in phases:
        return "failed"
    ranked = [PHASE_ORDER.index(p) for p in phases if p in PHASE_ORDER]
    if not ranked:
        return "pending"
    return PHASE_ORDER[min(ranked)]


def _revision(workload: Workload, deployments: list[Deployment]) -> str:
    """The value `If-Match` is checked against, across the workload and its expansion.

    A workload is one row plus N, and a scale that only moved a share touches a
    deployment and leaves `demand_workload.updated_at` where it was. Deriving the
    revision from the workload alone would let that write go unnoticed by the next
    conditional update, so the latest stamp anywhere in the object wins.
    """
    stamps = [workload.updated_at or workload.created_at]
    stamps.extend(d.updated_at or d.created_at for d in deployments)
    return str(int(max(stamps).timestamp() * 1_000_000))


def _status(row) -> DeploymentStatusSchema | None:
    if row is None:
        return None
    return DeploymentStatusSchema(
        observed_generation=row.observed_generation,
        phase=row.phase,
        reason=row.reason,
        message=row.message,
        desired_replicas=row.desired_replicas,
        ready_replicas=row.ready_replicas,
        updated_replicas=row.updated_replicas,
        unavailable_replicas=row.unavailable_replicas,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        last_error_at=row.last_error_at,
        observed_at=row.observed_at,
    )


def _rollup(row: dict | None) -> DeploymentReplicaRollupSchema:
    if not row:
        return EMPTY
    return DeploymentReplicaRollupSchema(
        total=row.get("total", 0),
        ready=row.get("ready", 0),
        running=row.get("running", 0),
        pending=row.get("pending", 0),
        failed=row.get("failed", 0),
        at_current_generation=row.get("at_current_generation", 0),
    )


class WorkloadService:
    """Create, read, update and delete the thing a user actually asked for.

    The service owns the commit; the repository only flushes. A workload is
    written together with the deployments placement expanded it into, in one
    transaction, because a workload whose expansion landed separately would be
    briefly indistinguishable from one nothing could place.
    """

    def __init__(self, db: AsyncSession, notifier: Notifier | None = None) -> None:
        self.db = db
        self.repo = WorkloadRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    async def _record(
        self,
        deployment: Deployment,
        event: str,
        message: str,
        details: dict[str, str] | None = None,
        severity: str = "info",
    ) -> None:
        """Files what the plane just did, in the caller's transaction.

        Before the caller's `commit()` on purpose: the event and the write it
        describes are one fact. The timeline is keyed by deployment because that is
        where a cluster's own account of the same object is filed, and a reader
        chasing a stuck workload should not have to consult two timelines.
        """
        await self.db.execute(
            statements.deployment_lifecycle(
                deployment.cluster_id,
                deployment.id,
                event,
                message,
                {"deployment_id": deployment.uid, "name": deployment.name, **(details or {})},
                dt.datetime.now(dt.UTC),
                severity,
            )
        )

    async def _trace(
        self,
        workload: Workload,
        event: str,
        message: str,
        details: dict[str, str] | None = None,
        severity: str = "info",
        cluster_id: UUID | None = None,
        moment: dt.datetime | None = None,
    ) -> None:
        """Files what the plane did to the workload itself, in the caller's transaction.

        The deployment timeline can only begin once there is a deployment, which is
        after placement has already answered. Everything before that — the request
        arriving, the clusters that were considered, the reason each one was ruled
        out — happens to the workload and has nowhere else to be filed.

        `moment` is passed by a caller that is filing several events for one call, so
        that the story reads in the order it happened rather than in the order the
        writes were issued.
        """
        await self.db.execute(
            statements.workload_lifecycle(
                workload.id,
                workload.uid,
                event,
                message,
                {"name": workload.name, **(details or {})},
                moment or dt.datetime.now(dt.UTC),
                severity,
                cluster_id,
            )
        )

    async def _trace_expansion(
        self,
        workload: Workload,
        decision,
        started: dt.datetime,
        decided: dt.datetime,
    ) -> None:
        """Files placement's whole answer against the workload: what it asked, and what it got.

        Placement already reasons per cluster and returns every verdict — the counts
        it decided on and the coded reasons a cluster lost. All of that was computed
        on every expansion and then dropped on the floor, except for one summary
        string copied onto the deployments that were created. A workload that fit
        nowhere therefore created no deployment, so the one case where the reasoning
        is the entire answer is the case that discarded all of it.

        The per-cluster rejections are filed only when the expansion did not fully
        land. A workload that placed cleanly does not need seven rows saying which
        clusters did not win; one that is short does, because those rows are the next
        action.

        Each rejection also carries `cluster_id`, so it appears on that cluster's own
        timeline. "Nothing will schedule here" is a fact about the cluster, and a
        reader looking at the cluster should not have to guess which workload to open
        to find it.
        """
        placed = sum(s.replicas for s in decision.shares)
        feasible = sum(1 for v in decision.verdicts if v.feasible)
        await self._trace(
            workload,
            "placing",
            (
                f"placement considered {len(decision.verdicts)} cluster(s) for {workload.device_count} × "
                f"{workload.device_class}; {feasible} could take it"
            ),
            {
                "candidates": str(len(decision.verdicts)),
                "feasible": str(feasible),
                "device_class": workload.device_class,
                "device_count": str(workload.device_count),
                "device_memory_mib_min": str(workload.device_memory_mib_min),
                "desired_scale": str(decision.desired),
                "took_ms": str(round((decided - started).total_seconds() * 1000)),
            },
            moment=started,
        )

        landed = decided
        spread = ", ".join(f"{s.cluster_name}×{s.replicas}" for s in decision.shares if s.replicas > 0)
        outcome = {
            "placed": str(placed),
            "desired": str(decision.desired),
            "shortfall": str(decision.shortfall),
            "clusters": spread,
            "summary": decision.summary or "",
        }
        if decision.desired == 0:
            await self._trace(
                workload,
                "held",
                decision.summary or f"{workload.name} asks for nothing; every share is 0",
                outcome,
                moment=landed,
            )
            return
        if placed == 0:
            await self._trace(
                workload,
                "unplaced",
                decision.summary or f"no cluster can take {workload.name}; nothing was deployed",
                outcome,
                severity="warning",
                moment=landed,
            )
        elif decision.shortfall > 0:
            await self._trace(
                workload,
                "shortfall",
                decision.summary or f"{placed} of {decision.desired} placed; {decision.shortfall} unplaced",
                outcome,
                severity="warning",
                moment=landed,
            )
        else:
            await self._trace(
                workload,
                "placed",
                decision.summary or f"{workload.name} was placed on {spread}",
                outcome,
                moment=landed,
            )
            return

        rejected = landed + dt.timedelta(microseconds=1)
        clusters = await self.repo.clusters_by_uid([v.cluster for v in decision.verdicts if not v.feasible])
        for verdict in decision.verdicts:
            if verdict.feasible:
                continue
            cluster = clusters.get(verdict.cluster)
            await self._trace(
                workload,
                "rejected",
                f"{verdict.cluster_name}: " + "; ".join(r.message for r in verdict.reasons),
                {
                    "cluster": verdict.cluster_name,
                    "codes": ",".join(r.code for r in verdict.reasons),
                    "free_devices": str(verdict.free_devices),
                    "reserved_devices": str(verdict.reserved_devices),
                    "available_devices": str(verdict.available_devices),
                    "largest_domain_free_devices": str(verdict.largest_domain_free_devices),
                    "quota_headroom": "" if verdict.quota_headroom is None else str(verdict.quota_headroom),
                },
                cluster_id=None if cluster is None else cluster.id,
                moment=rejected,
            )

    async def _announce(self, cluster_uid: str, uid: str, generation: int) -> None:
        """Wakes whichever hub holds this cluster's stream, after the write is committed.

        Every caller of this is on the far side of its own `commit()`: the hub
        answers the hint by re-reading the row, so announcing first would race the
        reader against a transaction that has not landed. Missing the wakeup costs
        latency and nothing else — the next resync carries the same state.
        """
        await self.notifier.changed(
            self.db,
            DeclaredChange(cluster_id=cluster_uid, kind=DEPLOYMENT_KIND, uid=uid, generation=generation),
        )

    def schema(
        self,
        workload: Workload,
        serving: WorkloadServingSchema,
        project_uid: str,
        priority_class: str,
        pinned_cluster_uid: str | None,
        adapter_uids: list[str],
        deployments: list[WorkloadDeploymentSchema],
        rows: list[Deployment],
        artifact_tag: str | None = None,
        served_model_name: str = "",
    ) -> WorkloadSchema:
        return WorkloadSchema(
            id=workload.uid,
            name=workload.name,
            kind=workload.kind,
            phase=derive_phase(rows),
            project_id=project_uid,
            priority_class=priority_class,
            preemptible=workload.preemptible,
            paused=workload.paused,
            gang_size=workload.gang_size,
            serving_digest=workload.serving_digest,
            serving=serving,
            adapter_artifact_ids=adapter_uids,
            served_model_name=served_model_name,
            overrides=workload.overrides,
            labels=workload.labels,
            strategy=workload.strategy,
            cluster_floors=workload.cluster_floors,
            max_clusters=workload.max_clusters,
            pinned_cluster_id=pinned_cluster_uid,
            scale=WorkloadScaleSchema(
                unit=UNITS.get(workload.kind, "replicas"),
                min=workload.min_scale,
                max=workload.max_scale,
                desired=workload.desired_scale,
                placed=sum(d.desired_replicas for d in deployments),
                ready=sum(d.replicas.ready for d in deployments),
            ),
            started_at=workload.started_at,
            finished_at=workload.finished_at,
            deadline_at=workload.deadline_at,
            created_at=workload.created_at,
            updated_at=workload.updated_at,
            revision=_revision(workload, rows),
            deployments=deployments,
            artifact_tag=artifact_tag,
        )

    def _deployment(self, row, replicas: DeploymentReplicaRollupSchema) -> WorkloadDeploymentSchema:
        deployment = row[0]
        return WorkloadDeploymentSchema(
            id=deployment.uid,
            name=deployment.name,
            cluster_id=row.cluster_uid,
            cluster_name=row.cluster_name,
            region=row.region,
            min_replicas=deployment.min_replicas,
            max_replicas=deployment.max_replicas,
            desired_replicas=deployment.desired_replicas,
            admission_phase=deployment.admission_phase,
            admission_reason=deployment.admission_reason,
            generation=deployment.generation,
            enabled=deployment.enabled,
            authority=deployment.authority,
            queued_at=deployment.queued_at,
            admitted_at=deployment.admitted_at,
            placed_at=deployment.placed_at,
            replicas=replicas,
            status=_status(row.DeploymentStatus),
        )

    async def _render(self, rows: list) -> list[WorkloadSchema]:
        """One page of workloads, their expansions, and their replica counts.

        Three queries whatever the page size, and never one per row.
        """
        if not rows:
            return []
        grouped = await self.repo.deployments([r[0].id for r in rows])
        deployment_rows = [d for group in grouped.values() for d in group]
        rollup = await self.repo.replicas([d[0].id for d in deployment_rows])
        adapters = await self.repo.adapter_uids([i for r in rows for i in r[0].adapter_artifact_ids])
        items = []
        for row in rows:
            workload = row[0]
            artifact = workload.artifact
            group = grouped.get(workload.id, [])
            items.append(
                self.schema(
                    workload=workload,
                    serving=serving_view(workload, artifact.uid),
                    project_uid=row.project_uid,
                    priority_class=row.priority_class,
                    pinned_cluster_uid=row.pinned_cluster_uid,
                    adapter_uids=[adapters[i] for i in workload.adapter_artifact_ids if i in adapters],
                    deployments=[self._deployment(d, _rollup(rollup.get(d[0].id))) for d in group],
                    rows=[d[0] for d in group],
                    artifact_tag=artifact.name,
                    served_model_name=served_model(workload, artifact),
                )
            )
        return items

    async def _one(self, ref: str, project: str | None = None):
        found = await self.repo.matches(ref, project)
        if not found:
            raise NotFound(f"no workload {ref!r}")
        if len(found) > 1:
            candidates = await self.repo.candidates(ref)
            raise Invalid(
                f"{ref!r} names a workload in more than one project; say which with `project`",
                {"candidates": [{"id": uid, "project": slug} for uid, slug in candidates]},
            )
        return found[0]

    async def get(self, ref: str, project: str | None = None) -> WorkloadSchema:
        rendered = await self._render([await self._one(ref, project)])
        return rendered[0]

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        kind: str | None = None,
        phase: str | None = None,
        artifact: str | None = None,
        serving_digest: str | None = None,
        device_class: str | None = None,
        cluster: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[WorkloadSchema]:
        if kind is not None and kind not in KINDS:
            raise Invalid(f"unknown kind {kind!r}", {"allowed": sorted(KINDS)})
        if phase is not None and phase not in PHASES:
            raise Invalid(f"unknown phase {phase!r}", {"allowed": sorted(PHASES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            project=project,
            kind=kind,
            phase=phase,
            artifact=artifact,
            serving_digest=serving_digest,
            device_class=device_class,
            cluster=cluster,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = await self._render(kept)
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def placement(self, ref: str, project: str | None = None) -> WorkloadPlacementSchema:
        """Where a workload physically landed, derived from its live allocations.

        Read-only over observed state: the allocation, its devices, and the nodes
        they sit on are all written by ingestion, and nothing here writes. Only
        membership and placement are reported — device counts, node occupancy, and
        the per-replica assignment — never any serving telemetry about them.
        """
        row = await self._one(ref, project)
        workload = row[0]
        rows = await self.repo.placement(workload.id)

        nodes: dict[str, WorkloadPlacementNodeSchema] = {}
        allocations: dict[str, dict] = {}
        devices: set[str] = set()
        for member in rows:
            devices.add(member.device_uid)
            node = nodes.get(member.node_uid)
            if node is None:
                nodes[member.node_uid] = WorkloadPlacementNodeSchema(
                    node=member.node_uid,
                    cluster=member.cluster_uid,
                    device_count=1,
                )
            else:
                node.device_count += 1
            allocation = allocations.get(member.allocation_uid)
            if allocation is None:
                allocations[member.allocation_uid] = {
                    "replica": member.replica_uid,
                    "cluster": member.cluster_uid,
                    "device_count": member.device_count,
                    "nodes": [member.node_uid],
                    "devices": [member.device_uid],
                }
            else:
                if member.node_uid not in allocation["nodes"]:
                    allocation["nodes"].append(member.node_uid)
                allocation["devices"].append(member.device_uid)

        replicas = [
            WorkloadReplicaPlacementSchema(
                allocation=uid,
                replica=allocation["replica"],
                cluster=allocation["cluster"],
                nodes=allocation["nodes"],
                devices=allocation["devices"],
                device_count=allocation["device_count"],
                gang_complete=len(allocation["devices"]) >= allocation["device_count"],
            )
            for uid, allocation in allocations.items()
        ]

        return WorkloadPlacementSchema(
            workload=workload.uid,
            node_count=len(nodes),
            gpu_count=len(devices),
            nodes=list(nodes.values()),
            replicas=replicas,
        )

    async def _adapter_ids(self, refs: list[str]) -> list[UUID]:
        if not refs:
            return []
        found = await self.repo.artifacts_by_ref(refs)
        if len(found) != len(refs):
            names = {a.uid for a in found} | {a.name for a in found}
            raise NotFound("unknown adapter artifact", {"missing": sorted(set(refs) - names)})
        return [a.id for a in found]

    async def _priority_class(self, ref: str | None, project) -> PriorityClass:
        """Resolves the priority class, falling back the way a scheduler must.

        Every workload carries one from day one, so an absent reference is not an
        absent priority: the project's default answers first, then the system
        default, and only a system with neither is a real error.
        """
        if ref is not None:
            found = await self.repo.priority_class_by_name(ref)
            if found is None:
                raise NotFound(f"no priority class {ref!r}")
            return found
        if project.default_priority_class_id is not None:
            found = await self.repo.priority_class(project.default_priority_class_id)
            if found is not None:
                return found
        fallback = await self.repo.default_priority_class()
        if fallback is None:
            raise Invalid("no priority class was given and none is marked default")
        return fallback

    async def _expand(
        self,
        workload: Workload,
        exclude: str | None = None,
        extra_policies: tuple[PolicySnapshot, ...] = (),
    ):
        """Asks placement where this workload goes, and how much of it goes there.

        The shares come back whole and are never re-derived here: a share is this
        cluster's count, the workload's totals are the sum of every share, and
        computing either from the other is the bug the layering exists to prevent.

        A partial expansion is not a failure. Fewer shares than the total asked for
        means the clusters that could take some of it took some of it, and the
        shortfall is visible in the scale rollup rather than costing the caller the
        capacity that was found. A gang is the one shape with no partial answer, and
        placement raises rather than returning half of one.

        `extra_policies` exists for exactly one caller: `create`, whose workload has no
        primary key yet — the column is `server_default` — so nothing can be attached to
        it and `_policies` has nothing to find. The constraint the caller declared is
        handed to the expansion directly and persisted as a real Policy in the same
        write, so this is the only expansion in the object's life that needs it. Every
        later one reads the attachment.
        """
        placement = PlacementService(self.db)
        snapshot = None
        if exclude is not None or extra_policies:
            demand = await placement.demand(workload)
            workload_id = getattr(workload, "id", None)
            capacity = await placement.repo.capacity(workload.project_id, demand.device_class, workload_id)
            snapshot = CapacitySnapshot(
                clusters=tuple(c for c in capacity.clusters if exclude is None or c.uid != exclude),
                quotas=capacity.quotas,
                policies=capacity.policies + extra_policies,
            )
        return await placement.expand(workload, snapshot)

    async def create(self, data: WorkloadCreateRequest) -> WorkloadSchema:
        """Creates a workload and the deployments placement expanded it into.

        A declared `allowed_regions` or `allowed_clusters` becomes a real Policy attached
        to the workload, written in this same transaction. It is also handed to the
        expansion directly, because placement runs here and the attachment cannot exist
        yet — `Workload.id` is `server_default`, so there is no primary key to attach to
        until `repo.add` has flushed. Those are the same constraint expressed twice for
        one call only; every expansion after this one reads the attachment.

        The serving block is validated before anything is placed, and its digest is
        derived from the row rather than taken from the caller. A digest somebody else
        computed is a claim about a configuration rather than a reading of one.
        """
        requested_at = dt.datetime.now(dt.UTC)
        if data.kind not in KINDS:
            raise Invalid(f"unknown kind {data.kind!r}", {"allowed": sorted(KINDS)})
        if data.strategy not in STRATEGIES:
            raise Invalid(f"unknown strategy {data.strategy!r}", {"allowed": sorted(STRATEGIES)})
        if data.gang_size < 1:
            raise Invalid("gang_size must be at least 1", {"gang_size": data.gang_size})
        if data.max_clusters is not None and data.max_clusters < 1:
            raise Invalid("max_clusters must be at least 1", {"max_clusters": data.max_clusters})
        check_scale(data.min_scale, data.desired_scale, data.max_scale, data.kind, data.gang_size)

        artifact = await self.repo.artifact_by_ref(data.serving.artifact)
        if artifact is None:
            raise NotFound(f"no artifact {data.serving.artifact!r}")
        check_serving_request(data.serving, artifact)
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        priority = await self._priority_class(data.priority_class, project)

        pinned = None
        if data.cluster is not None:
            pinned = await self.repo.cluster_by_ref(data.cluster)
            if pinned is None:
                raise NotFound(f"no cluster {data.cluster!r}")

        narrowed = []
        for ref in data.allowed_clusters:
            cluster = await self.repo.cluster_by_ref(ref)
            if cluster is None:
                raise NotFound(f"no cluster {ref!r}")
            narrowed.append(cluster)
        if pinned is not None and (data.allowed_regions or narrowed):
            raise Invalid(
                "cluster pins placement to one and an allow-list asks placement to choose",
                {"cluster": data.cluster},
            )
        constraint = (
            PolicySnapshot(
                name=constraint_name(data.name),
                enabled=True,
                allowed_regions=tuple(data.allowed_regions),
                allowed_residencies=(),
                allowed_device_classes=(),
                allowed_clusters=tuple(c.uid for c in narrowed),
                denied_clusters=(),
            )
            if data.allowed_regions or narrowed
            else None
        )

        workload = Workload(
            uid=new_uid(WORKLOAD),
            name=data.name,
            kind=data.kind,
            project_id=project.id,
            priority_class_id=priority.id,
            min_scale=data.min_scale,
            max_scale=data.max_scale,
            desired_scale=data.desired_scale,
            strategy=data.strategy,
            cluster_floors=data.cluster_floors,
            max_clusters=data.max_clusters,
            pinned_cluster_id=None if pinned is None else pinned.id,
            gang_size=data.gang_size,
            preemptible=data.preemptible,
            paused=False,
            adapter_artifact_ids=await self._adapter_ids(data.adapter_artifacts),
            overrides=data.overrides,
            labels=data.labels,
            deadline_at=data.deadline_at,
        )
        apply_serving(workload, data.serving, artifact)

        started = dt.datetime.now(dt.UTC)
        decision = await self._expand(workload, extra_policies=() if constraint is None else (constraint,))
        decided = dt.datetime.now(dt.UTC)
        shares = [s for s in decision.shares if s.replicas > 0]
        clusters = await self.repo.clusters_by_uid([s.cluster for s in shares])
        now = decided
        deployments = [
            Deployment(
                uid=new_uid(DEPLOYMENT),
                workload=workload,
                cluster_id=clusters[share.cluster].id,
                name=data.name,
                min_replicas=share.replicas,
                max_replicas=share.replicas,
                desired_replicas=share.replicas,
                admission_phase="placed",
                admission_reason=decision.summary or None,
                queued_at=now,
                placed_at=now,
                generation=1,
                enabled=data.enabled,
                authority=MANAGED,
            )
            for share in shares
        ]

        try:
            await self.repo.add(workload, deployments)
            if constraint is not None:
                self.db.add(
                    Policy(
                        uid=new_uid(POLICY),
                        name=constraint.name,
                        description=f"Placement constraint declared when {data.name} was created.",
                        allowed_regions=list(data.allowed_regions),
                        allowed_cluster_ids=[c.id for c in narrowed],
                        enabled=True,
                        attachments=[PolicyAttachment(subject_type=SUBJECT_WORKLOAD, subject_id=workload.id)],
                    )
                )
            await self._trace(
                workload,
                "requested",
                (
                    f"{data.name} was requested: {data.desired_scale} {UNITS[data.kind]} of "
                    f"{artifact.name} on {workload.device_count} × {workload.device_class}"
                ),
                {
                    "kind": data.kind,
                    "strategy": data.strategy,
                    "project": project.slug,
                    "artifact": artifact.uid,
                    "engine": workload.engine,
                    "engine_runtime_version": workload.engine_runtime_version,
                    "device_class": workload.device_class,
                    "device_count": str(workload.device_count),
                    "device_memory_mib_min": str(workload.device_memory_mib_min),
                    "tensor_parallel": str(workload.tensor_parallel),
                    "context_length": str(workload.context_length),
                    "desired_scale": str(data.desired_scale),
                    "cluster": data.cluster or "",
                    "serving_digest": workload.serving_digest,
                },
                moment=requested_at,
            )
            await self._trace_expansion(workload, decision, started, decided)
            for deployment, share in zip(deployments, shares, strict=True):
                await self._record(
                    deployment,
                    "created",
                    f"{data.name} was created on {share.cluster_name} serving {artifact.name}",
                    {
                        "cluster": share.cluster_name,
                        "artifact": artifact.uid,
                        "serving_digest": workload.serving_digest,
                        "workload_id": workload.uid,
                        "desired_replicas": str(share.replicas),
                        "placement_reason": decision.summary or "",
                    },
                )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"workload {data.name!r} already exists in {project.slug!r}") from exc

        for row in (await self.repo.deployments([workload.id])).get(workload.id, []):
            await self._announce(row.cluster_uid, row[0].uid, row[0].generation)
        return await self.get(workload.uid)

    async def _apply_serving_patch(self, workload: Workload, patch: WorkloadServingUpdate) -> None:
        """Merges a serving patch onto the stored configuration and rewrites the columns.

        The whole configuration is validated, not the fields the caller named, so a
        patch cannot reach a state a create would have refused — a runtime version
        bumped past what the shape can hold is caught here rather than by a cluster.

        `engine_runtime_image` re-derives when the stored one is the image this plane
        produced and the caller did not name a new one, so that moving
        `engine_runtime_version` moves the image. Carrying the stored image forward as a
        declared one made every workload pin itself the moment it was created: the
        version bump was accepted, stored, and silently ignored, and the workload
        reported the new version while the cluster went on pulling the old image. An
        image somebody pinned by hand does not match what the old engine and runtime
        version resolve to, so it survives a version bump untouched, and naming
        `engine_runtime_image` in the same patch always wins outright.
        """
        artifact = await self.repo.artifact_of(workload)
        if artifact is None:
            raise NotFound(f"workload {workload.uid} has no artifact")
        declared = {key: value for key, value in patch.to_dict().items() if value is not UNSET}
        was_derived = workload.engine_runtime_image == resolve_engine_runtime_image(
            workload.engine, workload.engine_runtime_version, None
        )
        request = serving_request(workload, artifact)
        for key, value in declared.items():
            setattr(request, key, value)
        if was_derived and "engine_runtime_image" not in declared:
            request.engine_runtime_image = None
        check_serving_request(request, artifact)
        apply_serving(workload, request, artifact)

    async def update(self, ref: str, data: WorkloadUpdateRequest, if_match: str | None = None) -> WorkloadSchema:
        """A partial update of what was asked for, globally.

        Scale is not here and cannot be: changing it re-expands the workload across
        clusters and can sit pending on quota indefinitely, which is the
        `workload.scale` operation. `strategy`, `cluster_floors` and `max_clusters`
        are declared here and take effect at the next expansion rather than
        reshuffling a running workload the moment they are written.

        A serving change bumps every deployment's generation, because it is the one
        kind of write here that changes the process a cluster is running.
        """
        row = await self._one(ref)
        workload = row[0]
        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])
        deployments = [d[0] for d in deployment_rows]
        revision = _revision(workload, deployments)
        if if_match is not None and if_match != revision:
            raise Conflict("workload changed since it was read", {"revision": revision})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "strategy" in fields and fields["strategy"] not in STRATEGIES:
            raise Invalid(f"unknown strategy {fields['strategy']!r}", {"allowed": sorted(STRATEGIES)})
        if fields.get("max_clusters") is not None and fields.get("max_clusters", 1) < 1:
            raise Invalid("max_clusters must be at least 1", {"max_clusters": fields["max_clusters"]})

        if "serving" in fields:
            await self._apply_serving_patch(workload, fields.pop("serving"))
        if "priority_class" in fields:
            name = fields.pop("priority_class")
            found = await self.repo.priority_class_by_name(name)
            if found is None:
                raise NotFound(f"no priority class {name!r}")
            workload.priority_class_id = found.id
        if "adapter_artifacts" in fields:
            workload.adapter_artifact_ids = await self._adapter_ids(fields.pop("adapter_artifacts"))
        for key, value in fields.items():
            setattr(workload, key, value)

        bumped = bool({"overrides", "adapter_artifacts", "labels", "serving"} & set(data.to_dict(exclude_unset=True)))
        if bumped:
            for deployment in deployments:
                deployment.generation += 1

        try:
            for deployment in deployments:
                await self._record(
                    deployment,
                    "updated",
                    f"{workload.name} was updated",
                    {
                        "fields": ", ".join(sorted(data.to_dict(exclude_unset=True))),
                        "workload_id": workload.uid,
                        "generation": str(deployment.generation),
                    },
                )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("workload conflicts with an existing one") from exc

        for deployment, deployment_row in zip(deployments, deployment_rows, strict=True):
            await self._announce(deployment_row.cluster_uid, deployment.uid, deployment.generation)
        return await self.get(workload.uid)

    async def _reconcile(self, workload: Workload, deployment_rows: list, exclude: str | None = None) -> dict:
        """Re-expands a workload and moves the per-cluster deployments onto the answer.

        Three outcomes per cluster, and they are deliberately not the same write: a
        cluster that keeps a share has its counts moved and its generation bumped so
        the agent re-reads; a cluster that appears gets a deployment; a cluster whose
        share fell to zero has its deployment deleted rather than parked at zero, so
        that "which clusters is this on" stays answerable by looking at the rows.

        The generation is bumped only when the share actually moved. Re-expanding a
        workload that landed on the same split is common — anything that touches the
        workload re-derives it — and making every one of those a new generation would
        have clusters restarting replicas to arrive where they already are.
        """
        started = dt.datetime.now(dt.UTC)
        decision = await self._expand(workload, exclude)
        now = dt.datetime.now(dt.UTC)
        await self._trace_expansion(workload, decision, started, now)
        shares = {s.cluster: s for s in decision.shares if s.replicas > 0}
        existing = {r.cluster_uid: r for r in deployment_rows}
        touched: list[tuple[str, str, int]] = []

        for cluster_uid, row in existing.items():
            deployment = row[0]
            share = shares.pop(cluster_uid, None)
            if share is None:
                await self._record(
                    deployment,
                    "deleted",
                    f"{workload.name} left {cluster_uid}",
                    {"workload_id": workload.uid},
                )
                await self.db.delete(deployment)
                touched.append((cluster_uid, deployment.uid, deployment.generation))
                continue
            if deployment.desired_replicas != share.replicas:
                deployment.min_replicas = share.replicas
                deployment.max_replicas = share.replicas
                deployment.desired_replicas = share.replicas
                deployment.generation += 1
                await self._record(
                    deployment,
                    "scaled",
                    f"{workload.name} was scaled to {share.replicas} on {share.cluster_name}",
                    {"workload_id": workload.uid, "generation": str(deployment.generation)},
                )
                touched.append((cluster_uid, deployment.uid, deployment.generation))

        if shares:
            clusters = await self.repo.clusters_by_uid(list(shares))
            for cluster_uid, share in shares.items():
                deployment = Deployment(
                    uid=new_uid(DEPLOYMENT),
                    workload=workload,
                    cluster_id=clusters[cluster_uid].id,
                    name=workload.name,
                    min_replicas=share.replicas,
                    max_replicas=share.replicas,
                    desired_replicas=share.replicas,
                    admission_phase="placed",
                    admission_reason=decision.summary or None,
                    queued_at=now,
                    admitted_at=now,
                    placed_at=now,
                    generation=1,
                    enabled=not workload.paused,
                    authority=MANAGED,
                )
                self.db.add(deployment)
                await self.db.flush()
                await self._record(
                    deployment,
                    "created",
                    f"{workload.name} was placed on {share.cluster_name}",
                    {"workload_id": workload.uid, "cluster": share.cluster_name},
                )
                touched.append((cluster_uid, deployment.uid, deployment.generation))

        return {
            "summary": decision.summary,
            "shares": {s.cluster: s.replicas for s in decision.shares if s.replicas > 0},
            "touched": [uid for _, uid, _ in touched],
            "_announce": touched,
        }

    async def _finish(self, result: dict) -> dict:
        """Commits the reconciliation, then wakes each cluster whose share moved."""
        announcements = result.pop("_announce")
        await self.db.commit()
        for cluster_uid, uid, generation in announcements:
            await self._announce(cluster_uid, uid, generation)
        return result

    async def rescale(self, ref: str, parameters: dict) -> dict:
        """Changes what was asked for, then re-expands it across clusters.

        The totals are validated against the kind before anything is placed, so a
        gang asked for a rank count its `gang_size` cannot honour is refused here
        rather than discovered by an engine that has already started moving shares.

        Absent parameters leave their bound alone. Sending only `desired` is the
        common case and must not silently widen or narrow the range around it.
        """
        row = await self._one(ref)
        workload = row[0]
        min_scale = parameters.get("min", workload.min_scale)
        max_scale = parameters.get("max", workload.max_scale)
        desired = parameters.get("desired", workload.desired_scale)
        for name, value in (("min", min_scale), ("max", max_scale), ("desired", desired)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise Invalid(f"{name} must be an integer", {name: value})
        check_scale(min_scale, desired, max_scale, workload.kind, workload.gang_size)

        workload.min_scale = min_scale
        workload.max_scale = max_scale
        workload.desired_scale = desired
        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])
        return await self._finish(await self._reconcile(workload, deployment_rows))

    async def set_paused(self, ref: str, paused: bool) -> dict:
        """Parks a workload without forgetting what it was, or brings it back.

        Pausing disables every cluster's copy rather than deleting them or zeroing
        the workload's totals: the shares are what the workload should return to,
        and a pause that destroyed them would make resuming a re-placement with a
        different answer. Resuming re-expands, because the clusters that could take
        it may not be the ones that could when it was parked.

        Both halves announce every deployment they touched, and the resuming half
        has to say so itself. `_reconcile` announces a cluster only when its share
        moved, and a pause leaves the shares exactly where they were — so a workload
        that comes back to the split it left moves nothing, announces nothing, and
        the clusters never hear that the deployments are enabled again. The row read
        `enabled` while the cluster still had no copy of it.
        """
        row = await self._one(ref)
        workload = row[0]
        if workload.paused == paused:
            return {"paused": paused, "unchanged": True}
        workload.paused = paused
        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])

        if paused:
            announcements = []
            for deployment_row in deployment_rows:
                deployment = deployment_row[0]
                deployment.enabled = False
                deployment.generation += 1
                await self._record(
                    deployment,
                    "paused",
                    f"{workload.name} was paused",
                    {"workload_id": workload.uid, "generation": str(deployment.generation)},
                )
                announcements.append((deployment_row.cluster_uid, deployment.uid, deployment.generation))
            await self.db.commit()
            for cluster_uid, uid, generation in announcements:
                await self._announce(cluster_uid, uid, generation)
            return {"paused": True}

        for deployment_row in deployment_rows:
            deployment_row[0].enabled = True
            deployment_row[0].generation += 1
        result = await self._reconcile(workload, deployment_rows)
        announced = {uid for _, uid, _ in result["_announce"]}
        result["_announce"].extend(
            (deployment_row.cluster_uid, deployment_row[0].uid, deployment_row[0].generation)
            for deployment_row in deployment_rows
            if deployment_row[0].uid not in announced
        )
        result["paused"] = False
        return await self._finish(result)

    async def drain(self, ref: str, cluster: str | None) -> dict:
        """Takes a workload off one cluster and re-expands what is left elsewhere.

        A drain is not a scale-down: the totals are untouched, so the share the
        drained cluster was carrying is offered to the others. If none of them can
        take it the workload runs short, which is visible in the scale rollup and is
        the honest outcome — quietly lowering what was asked for would hide it.

        Nothing here permanently excludes the cluster. Draining is a move, not a
        policy; keeping a workload off a cluster for good is what a Policy on it is
        for, and expressing it twice would give the two ways to disagree.
        """
        if not cluster:
            raise Invalid("workload.drain needs a cluster", {"parameters": ["cluster"]})
        row = await self._one(ref)
        workload = row[0]
        found = await self.repo.cluster_by_ref(cluster)
        if found is None:
            raise NotFound(f"no cluster {cluster!r}")

        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])
        draining = [r for r in deployment_rows if r[0].cluster_id == found.id]
        if not draining:
            raise Invalid(
                f"{workload.name} is not on cluster {cluster!r}",
                {"workload": workload.uid, "cluster": cluster},
            )

        announcements = []
        for deployment_row in draining:
            deployment = deployment_row[0]
            await self._record(
                deployment,
                "drained",
                f"{workload.name} was drained from {cluster}",
                {"workload_id": workload.uid},
            )
            await self.db.delete(deployment)
            announcements.append((deployment_row.cluster_uid, deployment.uid, deployment.generation))
        await self.db.flush()

        remaining = [r for r in deployment_rows if r[0].cluster_id != found.id]
        result = await self._reconcile(workload, remaining, exclude=found.uid)
        result["_announce"] = announcements + result["_announce"]
        result["drained"] = cluster
        return await self._finish(result)

    async def delete(self, ref: str) -> None:
        """Deletes the workload and every cluster's copy of it.

        Replicas cascade too, and that is correct: they are a cluster's report about
        a thing that no longer exists. Ingestion writing them again before the
        cluster has caught up is a reconciliation lag, not a resurrection.
        """
        row = await self._one(ref)
        workload = row[0]
        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])
        deployments = [d[0] for d in deployment_rows]
        announcements = [(r.cluster_uid, r[0].uid, r[0].generation) for r in deployment_rows]
        for deployment in deployments:
            await self._record(
                deployment,
                "deleted",
                f"{workload.name} was deleted",
                {"workload_id": workload.uid},
            )
        await self._trace(
            workload,
            "deleted",
            f"{workload.name} was deleted from {len(deployments)} cluster(s)",
            {"deployments": str(len(deployments))},
        )
        await self.repo.delete(workload, deployments)
        await self.db.commit()
        for cluster_uid, uid, generation in announcements:
            await self._announce(cluster_uid, uid, generation)
