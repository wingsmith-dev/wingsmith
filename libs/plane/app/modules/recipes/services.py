from dataclasses import replace

import msgspec

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import devices
from app.config import settings
from app.core.ids import RECIPE
from app.core.ids import new_uid
from app.core.vram import Sizing
from app.core.vram import sizing_of
from app.core.vram import impossible
from app.db.models import Recipe
from app.db.models import Artifact
from app.db.notify import RECIPE as RECIPE_KIND
from app.db.notify import Notifier
from app.db.notify import DeclaredChange
from app.db.notify import DatabaseNotifier
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.benchmarks import summaries

from .schemas import RecipeSchema
from .schemas import RecipeCacheSchema
from .schemas import RecipeShapeSchema
from .schemas import RecipeUsageSchema
from .schemas import RecipeCreateRequest
from .schemas import RecipeOverlaySchema
from .schemas import RecipeServingSchema
from .schemas import RecipeUpdateRequest
from .schemas import RecipeReasoningSchema
from .schemas import RecipeResourcesSchema
from .schemas import RecipeParallelismSchema
from .schemas import RecipeCapabilitiesSchema
from .schemas import RecipeBenchmarkSummarySchema
from .repository import RecipeRepository


LIFECYCLE_VALUES = ("draft", "published", "deprecated")


def check_lifecycle(value: str) -> None:
    """Rejects a lifecycle that is not one of the declared publication states."""
    if value not in LIFECYCLE_VALUES:
        raise Invalid(
            f"lifecycle must be one of {', '.join(LIFECYCLE_VALUES)}",
            {"lifecycle": value},
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
    rather than placement constraints: none of them decide which devices a recipe
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
    """The artifact's sizing facts, at the precision this recipe means to serve it.

    The recipe's quantization wins wherever it declares one. Serving fp16 weights
    at fp8 halves what has to be resident, so sizing a recipe against the repo's
    native precision would refuse shapes that work — the recipe is the thing that
    decides how the weights are loaded, and the artifact only says how they ship.
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
    estimate would eventually refuse a recipe that serves perfectly well.

    Silent about anything it cannot measure. An artifact whose repo published no
    attention shape or parameter count, and a device class the catalogue does not
    name, both pass — the plane does not refuse what it has no number for.

    `tensor_parallel` is the recipe's own, not the device count. A replica of sixteen
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


def _members(name: str, values: list[str], allowed: frozenset[str]) -> None:
    unknown = [v for v in values if v not in allowed]
    if unknown:
        raise Invalid(f"unknown {name}: {', '.join(sorted(unknown))}", {name: values, "allowed": sorted(allowed)})
    if len(set(values)) != len(values):
        raise Invalid(f"{name} is a set and repeats a member", {name: values})


def _reasoning_declared(reasoning: RecipeReasoningSchema) -> bool:
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
    serving: RecipeServingSchema,
    capabilities: RecipeCapabilitiesSchema,
    prefix_cache_enabled: bool,
) -> None:
    """Rejects a serving configuration that contradicts itself.

    Every rule here is a consistency check between two things the recipe declares
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
            "a recipe that is not claimed to reason cannot declare how it reasons",
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
            "a tool call parser is meaningless on a recipe that does not claim tool calling",
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


def serving_args(engine: str, serving: RecipeServingSchema) -> dict[str, str]:
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


def engine_image_repos() -> dict[str, str]:
    """`ENGINE_IMAGE_REPOS` parsed into `engine -> repository`.

    Malformed pairs are dropped rather than raised on: this is read on the write
    path of every recipe, and one bad character in an environment variable must
    not be the reason a recipe cannot be created.
    """
    repos: dict[str, str] = {}
    for pair in settings.ENGINE_IMAGE_REPOS.split(","):
        engine, _, repository = pair.strip().partition("=")
        if engine and repository:
            repos[engine.strip().lower()] = repository.strip()
    return repos


def resolve_engine_image(engine: str, engine_version: str, declared: str | None) -> str | None:
    """The image a recipe serves with, resolved from its engine and version.

    A declared image is returned untouched — pinning an exact image is the whole
    reason the field is writable, and a recipe that names one has already said
    what it means. Everything else is the common case: an author picks `vllm` and
    a version, and the repository those two imply is configuration, not a thing
    to retype.

    Returns `None` for an engine with no configured repository, which leaves the
    recipe exactly as it was before this existed. That is deliberate: inventing an
    address for an engine the plane has never been told about would trade a
    visible "no image" for an `ImagePullBackOff` on a cluster.
    """
    if declared:
        return declared
    repository = engine_image_repos().get((engine or "").strip().lower())
    if not repository:
        return None
    tag = (engine_version or "").strip()
    if not tag or tag == "latest":
        tag = settings.ENGINE_IMAGE_TAG_DEFAULT
    return f"{repository}:{tag}"


def resolved_engine_args(engine: str, engine_args: dict | None, serving: RecipeServingSchema) -> dict[str, str]:
    """`engine_args` with the typed serving flags laid over the top.

    The typed field wins the collision. `engine_args` stays the escape hatch for a
    flag nothing here models, but a flag that *is* modelled has one declared home,
    and a recipe that pins `--reasoning-parser` by hand while also naming
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
    resolved.update(serving_args(engine, serving))
    return resolved


def serving_block(stored: dict | None) -> RecipeServingSchema:
    return msgspec.convert(stored or {}, RecipeServingSchema)


def capabilities_block(stored: dict | None) -> RecipeCapabilitiesSchema:
    return msgspec.convert(stored or {}, RecipeCapabilitiesSchema)


def _revision(recipe: Recipe) -> str:
    stamp = recipe.updated_at or recipe.created_at
    return str(int(stamp.timestamp() * 1_000_000))


class RecipeService:
    def __init__(self, db: AsyncSession, notifier: Notifier | None = None) -> None:
        self.db = db
        self.repo = RecipeRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    async def _announce(self, recipe: Recipe) -> None:
        """Wakes every hub holding a cluster that runs this recipe, after the commit.

        A deployment write knows its one cluster; a recipe is global and knows
        none, so the fan-out is derived — one hint per cluster with a deployment
        naming it. A recipe nothing deploys yet announces to nobody, which is the
        correct amount of noise for a change no cluster can observe.

        Called on the far side of `commit()` for the same reason the deployment
        path is: the hub answers a hint by re-reading the row.
        """
        for cluster_uid in await self.repo.clusters_running(recipe):
            await self.notifier.changed(
                self.db,
                DeclaredChange(cluster_id=cluster_uid, kind=RECIPE_KIND, uid=recipe.uid),
            )

    def schema(self, recipe: Recipe, artifact_uid: str) -> RecipeSchema:
        fraction = recipe.kv_cache_fraction
        serving = serving_block(recipe.serving)
        return RecipeSchema(
            id=recipe.uid,
            artifact_id=artifact_uid,
            name=recipe.name,
            version=recipe.version,
            lifecycle=recipe.lifecycle or "published",
            engine=recipe.engine,
            engine_version=recipe.engine_version,
            engine_image=recipe.engine_image,
            engine_args=recipe.engine_args,
            imported_args=recipe.imported_args,
            quantization=recipe.quantization,
            dtype=recipe.dtype,
            parallelism=RecipeParallelismSchema(
                tensor=recipe.tensor_parallel,
                pipeline=recipe.pipeline_parallel,
                expert=recipe.expert_parallel,
            ),
            context_length=recipe.context_length,
            max_batch_tokens=recipe.max_batch_tokens,
            cache=RecipeCacheSchema(
                kv_cache_dtype=recipe.kv_cache_dtype,
                kv_cache_fraction=None if fraction is None else float(fraction),
                prefix_cache_enabled=recipe.prefix_cache_enabled,
                prefix_cache_config=recipe.prefix_cache_config,
            ),
            serving=serving,
            capabilities=capabilities_block(recipe.capabilities),
            resolved_engine_args=resolved_engine_args(recipe.engine, recipe.engine_args, serving),
            shape=RecipeShapeSchema(
                device_class=recipe.device_class,
                device_count=recipe.device_count,
                device_memory_mib_min=recipe.device_memory_mib_min,
                requires_single_topology_domain=recipe.requires_single_topology_domain,
                topology_domain_kind=recipe.topology_domain_kind,
            ),
            resources=RecipeResourcesSchema(
                cpu_cores=recipe.cpu_cores,
                memory_mib=recipe.memory_mib,
                disk_gib=recipe.disk_gib,
            ),
            overlay=RecipeOverlaySchema(
                pod_spec=(recipe.overlay or {}).get("pod_spec", {}) or {},
                dynamo=(recipe.overlay or {}).get("dynamo", {}) or {},
            ),
            labels=recipe.labels,
            created_at=recipe.created_at,
            updated_at=recipe.updated_at,
            revision=_revision(recipe),
        )

    async def _one(self, ref: str) -> tuple[Recipe, str]:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no recipe {ref!r}")
        return row[0], row.artifact_uid

    async def get(self, ref: str) -> RecipeSchema:
        return self.schema(*await self._one(ref))

    async def usage(self, ref: str) -> RecipeUsageSchema:
        """How many workloads name this recipe, counted at read time."""
        recipe, _ = await self._one(ref)
        return RecipeUsageSchema(
            recipe_id=recipe.uid,
            workload_count=await self.repo.workload_usage_count(recipe),
        )

    async def benchmarks_summary(self, ref: str) -> RecipeBenchmarkSummarySchema:
        """Peak per-stream output-token throughput across this recipe's runs.

        Offline benchmark measurement, read through the same harness-and-version
        adapter the verdicts use. A run whose harness or version has no adapter,
        or whose summary lacks the throughput path, contributes no reading and is
        left out of `graded_run_count` — the peak is the maximum of what could be
        read, or `None` when nothing could.
        """
        recipe, _ = await self._one(ref)
        rows = await self.repo.benchmark_runs(recipe)
        peak: float | None = None
        graded = 0
        for row in rows:
            adapter = summaries.adapter_for(row.harness, row.harness_version)
            if adapter is None:
                continue
            reading = summaries.read(adapter, row.summary, summaries.OUTPUT_SPEED_PER_STREAM)
            if reading.value is None:
                continue
            graded += 1
            peak = reading.value if peak is None else max(peak, reading.value)
        return RecipeBenchmarkSummarySchema(
            recipe_id=recipe.uid,
            run_count=len(rows),
            graded_run_count=graded,
            peak_output_tokens_per_second=peak,
        )

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        artifact: str | None = None,
        engine: str | None = None,
        device_class: str | None = None,
        lifecycle: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[RecipeSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            artifact=artifact,
            engine=engine,
            device_class=device_class,
            lifecycle=lifecycle,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(r[0], r.artifact_uid) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: RecipeCreateRequest) -> RecipeSchema:
        check_shape(
            device_class=data.device_class,
            device_count=data.device_count,
            tensor_parallel=data.tensor_parallel,
            pipeline_parallel=data.pipeline_parallel,
            expert_parallel=data.expert_parallel,
            context_length=data.context_length,
            kv_cache_fraction=data.kv_cache_fraction,
        )
        check_footprint(
            device_memory_mib_min=data.device_memory_mib_min,
            max_batch_tokens=data.max_batch_tokens,
            cpu_cores=data.cpu_cores,
            memory_mib=data.memory_mib,
            disk_gib=data.disk_gib,
        )
        check_serving(data.serving, data.capabilities, data.prefix_cache_enabled)
        check_lifecycle(data.lifecycle)
        check_quantization(data.quantization)
        if data.version < 1:
            raise Invalid("version must be at least 1", {"version": data.version})
        artifact = await self.repo.artifact_by_ref(data.artifact)
        if artifact is None:
            raise NotFound(f"no artifact {data.artifact!r}")
        check_capacity(
            sizing_for(artifact, data.quantization),
            data.device_class,
            data.device_count,
            data.tensor_parallel,
        )

        recipe = Recipe(
            uid=new_uid(RECIPE),
            artifact_id=artifact.id,
            name=data.name,
            version=data.version,
            lifecycle=data.lifecycle,
            engine=data.engine,
            engine_version=data.engine_version,
            engine_image=resolve_engine_image(data.engine, data.engine_version, data.engine_image),
            engine_args=data.engine_args,
            imported_args=data.imported_args,
            quantization=data.quantization,
            dtype=data.dtype,
            tensor_parallel=data.tensor_parallel,
            pipeline_parallel=data.pipeline_parallel,
            expert_parallel=data.expert_parallel,
            context_length=data.context_length,
            max_batch_tokens=data.max_batch_tokens,
            kv_cache_dtype=data.kv_cache_dtype,
            kv_cache_fraction=data.kv_cache_fraction,
            prefix_cache_enabled=data.prefix_cache_enabled,
            prefix_cache_config=data.prefix_cache_config,
            device_class=data.device_class,
            device_count=data.device_count,
            device_memory_mib_min=data.device_memory_mib_min,
            requires_single_topology_domain=data.requires_single_topology_domain,
            topology_domain_kind=data.topology_domain_kind,
            cpu_cores=data.cpu_cores,
            memory_mib=data.memory_mib,
            disk_gib=data.disk_gib,
            labels=data.labels,
            overlay=data.overlay,
            serving=msgspec.to_builtins(data.serving),
            capabilities=msgspec.to_builtins(data.capabilities),
        )
        try:
            await self.repo.add(recipe)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"recipe {data.name!r} version {data.version} already exists") from exc
        await self._announce(recipe)
        return await self.get(recipe.uid)

    async def update(self, ref: str, data: RecipeUpdateRequest, if_match: str | None = None) -> RecipeSchema:
        recipe, _ = await self._one(ref)
        if if_match is not None and if_match != _revision(recipe):
            raise Conflict("recipe changed since it was read", {"revision": _revision(recipe)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        declared_serving = fields.pop("serving", None)
        declared_capabilities = fields.pop("capabilities", None)
        serving = serving_block(recipe.serving) if declared_serving is None else declared_serving
        capabilities = (
            capabilities_block(recipe.capabilities) if declared_capabilities is None else declared_capabilities
        )
        check_serving(serving, capabilities, fields.get("prefix_cache_enabled", recipe.prefix_cache_enabled))
        if "lifecycle" in fields:
            check_lifecycle(fields["lifecycle"])
        if "quantization" in fields:
            check_quantization(fields["quantization"])
        check_shape(
            device_class=fields.get("device_class", recipe.device_class),
            device_count=fields.get("device_count", recipe.device_count),
            tensor_parallel=fields.get("tensor_parallel", recipe.tensor_parallel),
            pipeline_parallel=fields.get("pipeline_parallel", recipe.pipeline_parallel),
            expert_parallel=fields.get("expert_parallel", recipe.expert_parallel),
            context_length=fields.get("context_length", recipe.context_length),
            kv_cache_fraction=fields.get("kv_cache_fraction", recipe.kv_cache_fraction),
        )
        check_footprint(
            **{
                name: fields[name]
                for name in ("device_memory_mib_min", "max_batch_tokens", "cpu_cores", "memory_mib", "disk_gib")
                if name in fields
            }
        )
        artifact = await self.repo.artifact_of(recipe)
        if artifact is not None:
            check_capacity(
                sizing_for(artifact, fields.get("quantization", recipe.quantization)),
                fields.get("device_class", recipe.device_class),
                fields.get("device_count", recipe.device_count),
                fields.get("tensor_parallel", recipe.tensor_parallel),
            )
        for key, value in fields.items():
            setattr(recipe, key, value)
        recipe.engine_image = resolve_engine_image(recipe.engine, recipe.engine_version, recipe.engine_image)
        if declared_serving is not None:
            recipe.serving = msgspec.to_builtins(serving)
        if declared_capabilities is not None:
            recipe.capabilities = msgspec.to_builtins(capabilities)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("recipe conflicts with an existing one") from exc
        await self._announce(recipe)
        return await self.get(recipe.uid)

    async def delete(self, ref: str) -> None:
        """Refuses while a deployment still names the recipe.

        `demand_deployment.recipe_id` is `RESTRICT`: a running deployment must
        keep the description of what it is running.
        """
        recipe, _ = await self._one(ref)
        try:
            await self.repo.delete(recipe)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"recipe {ref!r} is still referenced by a deployment") from exc
