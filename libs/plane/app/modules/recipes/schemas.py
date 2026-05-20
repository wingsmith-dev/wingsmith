import datetime as dt

from msgspec import field

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class RecipeShapeSchema(Struct):
    """The hardware a recipe implies, class first.

    `device_class` is not optional and is not separable from the counts: a
    tensor-parallel-8 recipe means nothing spread across an A100 and an H100, so
    a count without a class is not a shape.
    """

    device_class: str
    device_count: int
    device_memory_mib_min: int
    requires_single_topology_domain: bool
    topology_domain_kind: str | None


class RecipeParallelismSchema(Struct):
    tensor: int
    pipeline: int
    expert: int


class RecipeCacheSchema(Struct):
    """KV and prefix cache configuration. Unrelated to the weight cache.

    The weight cache is a declared warm-set per cluster with observed residency;
    this is per-request attention state and is configuration on the recipe. They
    are two different things and do not share an object.
    """

    kv_cache_dtype: str | None
    kv_cache_fraction: float | None
    prefix_cache_enabled: bool
    prefix_cache_config: dict


class RecipeReasoningSchema(Struct):
    """How this recipe thinks, as rendered flags. What it *can* do is in `capabilities`.

    Its own struct for the reason `RecipeCacheSchema` is: the fields are
    meaningless individually. All of them are conditioned on the model being able
    to think at all, and flattened onto the serving block they would read as six
    unrelated optionals with nothing to group them under.
    """

    default_on: bool = False
    parser: str | None = None
    transport: str = "reasoning_content"
    think_begin_token: str | None = None
    think_end_token: str | None = None
    suppress_eos_until_think_end: bool = False


class RecipeServingSchema(Struct):
    """What the served API does, as the engine will actually be told to behave.

    Every field renders to a flag or a rendered value. Honouring any of it means
    a replica restart, which is why it lives beside `cache` on the recipe rather
    than on the endpoint. Before it existed all of this went into `engine_args`,
    where the plane could neither validate it nor say what it had asked for.

    Every default is the behaviour of a recipe that says nothing, so the block is
    inert on an existing recipe: an empty parser, an empty tool parser and
    `suppress_eos_until_think_end` false all mean "emit no flag".
    """

    reasoning: RecipeReasoningSchema = field(default_factory=RecipeReasoningSchema)
    stream_usage: str = "final_chunk"
    context_overflow: str = "reject"
    structured_output_backend: str | None = None
    tool_call_parser: str | None = None
    max_images_per_request: int | None = None


class RecipeCapabilitiesSchema(Struct):
    """What this engine × artifact is claimed to be able to do. Asserted, never rendered.

    The same split as `engine_args` against `imported_args`, one level up: nothing
    here becomes a flag or reaches a cluster, because a cluster has no use for a
    claim about itself. The plane checks these against the recipe's own
    declarations and against nothing else — it holds no table of what an engine
    can do — so a block that is internally consistent and entirely false is
    accepted, stored, and falsified by the first conformance run.

    `asserted_by` / `asserted_at` / `source_uri` are the provenance triple, spelled
    the same way and in the same order as every other asserted claim in the model.
    """

    reasoning_supported: bool = False
    streaming_supported: bool = True
    structured_output_modes: list[str] = field(default_factory=lambda: ["text"])
    tool_calling_supported: bool = False
    tool_choice_modes: list[str] = field(default_factory=list)
    parallel_tool_calls: bool = False
    input_modalities: list[str] = field(default_factory=lambda: ["text"])
    prompt_cache_mode: str = "off"
    prompt_cache_ttl_seconds: int | None = None
    asserted_by: str | None = None
    asserted_at: dt.datetime | None = None
    source_uri: str | None = None


class RecipeResourcesSchema(Struct):
    cpu_cores: int | None
    memory_mib: int | None
    disk_gib: int | None


class RecipeOverlaySchema(Struct):
    """Passthrough for what the plane deliberately does not model.

    `pod_spec` merges over the rendered pod with strategic-merge semantics;
    `dynamo` merges over the rendered graph deployment, where maps merge and
    lists replace.
    """

    pod_spec: dict
    dynamo: dict


class RecipeSchema(Struct):
    """`resolved_engine_args` is derived on every read and stored nowhere.

    It is the recipe's declared `engine_args` with the flags the typed `serving`
    block dictates laid over the top, keyed the way the operator keys them —
    without the leading dashes. It exists so that "which value actually took
    effect" is a thing the console can show rather than a thing a reader has to
    reconstruct from two fields with overlapping meaning.
    """

    id: str
    artifact_id: str
    name: str
    version: int
    lifecycle: str
    engine: str
    engine_version: str
    engine_image: str | None
    engine_args: dict
    imported_args: dict
    served_model_name: str | None
    resolved_served_model_name: str
    quantization: str | None
    dtype: str | None
    parallelism: RecipeParallelismSchema
    context_length: int
    max_batch_tokens: int | None
    cache: RecipeCacheSchema
    serving: RecipeServingSchema
    capabilities: RecipeCapabilitiesSchema
    resolved_engine_args: dict
    shape: RecipeShapeSchema
    resources: RecipeResourcesSchema
    overlay: RecipeOverlaySchema
    labels: dict
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str


class RecipeUsageSchema(Struct):
    """How many workloads name this recipe, derived at read time.

    A count rather than the workloads themselves: the question a recipe answers
    here is whether anything depends on it, which is also the reason a delete is
    refused. Nothing is stored — the number is a live count of
    `Workload.recipe_id` against this recipe.
    """

    recipe_id: str
    workload_count: int


class RecipeBenchmarkSummarySchema(Struct):
    """Offline benchmark measurement rolled up per recipe, computed at read time.

    `peak_output_tokens_per_second` is the highest per-stream output-token
    throughput any graded run of this recipe reported, read through the same
    harness-and-version adapter the verdicts use and never through a normalised
    metric schema. It is `None` when no run carried a reading this plane knows
    how to find — a named absence, not a zero. This is held-in-plane offline
    measurement, never live serving telemetry.
    """

    recipe_id: str
    run_count: int
    graded_run_count: int
    peak_output_tokens_per_second: float | None


class RecipeCreateRequest(Struct):
    artifact: str
    name: str
    engine: str
    engine_version: str
    device_class: str
    context_length: int
    version: int = 1
    lifecycle: str = "published"
    engine_image: str | None = None
    engine_args: dict = {}
    imported_args: dict = {}
    served_model_name: str | None = None
    quantization: str | None = None
    dtype: str | None = None
    tensor_parallel: int = 1
    pipeline_parallel: int = 1
    expert_parallel: int = 1
    max_batch_tokens: int | None = None
    kv_cache_dtype: str | None = None
    kv_cache_fraction: float | None = None
    prefix_cache_enabled: bool = True
    prefix_cache_config: dict = {}
    device_count: int = 1
    device_memory_mib_min: int = 0
    requires_single_topology_domain: bool = False
    topology_domain_kind: str | None = None
    cpu_cores: int | None = None
    memory_mib: int | None = None
    disk_gib: int | None = None
    labels: dict = {}
    overlay: dict = {}
    serving: RecipeServingSchema = field(default_factory=RecipeServingSchema)
    capabilities: RecipeCapabilitiesSchema = field(default_factory=RecipeCapabilitiesSchema)


class RecipeUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `artifact` and `version` are absent by design. A recipe is a reproducible way
    to serve some specific weights; repointing it at other weights, or reusing a
    version number for different configuration, makes every deployment that
    names it mean something it did not mean yesterday.
    """

    name: str | UnsetType = UNSET
    lifecycle: str | UnsetType = UNSET
    engine: str | UnsetType = UNSET
    engine_version: str | UnsetType = UNSET
    engine_image: str | None | UnsetType = UNSET
    engine_args: dict | UnsetType = UNSET
    imported_args: dict | UnsetType = UNSET
    served_model_name: str | None | UnsetType = UNSET
    quantization: str | None | UnsetType = UNSET
    dtype: str | None | UnsetType = UNSET
    tensor_parallel: int | UnsetType = UNSET
    pipeline_parallel: int | UnsetType = UNSET
    expert_parallel: int | UnsetType = UNSET
    context_length: int | UnsetType = UNSET
    max_batch_tokens: int | None | UnsetType = UNSET
    kv_cache_dtype: str | None | UnsetType = UNSET
    kv_cache_fraction: float | None | UnsetType = UNSET
    prefix_cache_enabled: bool | UnsetType = UNSET
    prefix_cache_config: dict | UnsetType = UNSET
    device_class: str | UnsetType = UNSET
    device_count: int | UnsetType = UNSET
    device_memory_mib_min: int | UnsetType = UNSET
    requires_single_topology_domain: bool | UnsetType = UNSET
    topology_domain_kind: str | None | UnsetType = UNSET
    cpu_cores: int | None | UnsetType = UNSET
    memory_mib: int | None | UnsetType = UNSET
    disk_gib: int | None | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
    overlay: dict | UnsetType = UNSET
    serving: RecipeServingSchema | UnsetType = UNSET
    capabilities: RecipeCapabilitiesSchema | UnsetType = UNSET
