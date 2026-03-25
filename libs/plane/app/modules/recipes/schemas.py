import datetime as dt

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
    id: str
    artifact_id: str
    name: str
    version: int
    engine: str
    engine_version: str
    engine_image: str | None
    engine_args: dict
    quantization: str | None
    dtype: str | None
    parallelism: RecipeParallelismSchema
    context_length: int
    max_batch_tokens: int | None
    cache: RecipeCacheSchema
    shape: RecipeShapeSchema
    resources: RecipeResourcesSchema
    overlay: RecipeOverlaySchema
    labels: dict
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str


class RecipeCreateRequest(Struct):
    artifact: str
    name: str
    engine: str
    engine_version: str
    device_class: str
    context_length: int
    version: int = 1
    engine_image: str | None = None
    engine_args: dict = {}
    quantization: str | None = None
    dtype: str | None = None
    tensor_parallel: int = 1
    pipeline_parallel: int = 1
    expert_parallel: int = 1
    max_batch_tokens: int | None = None
    kv_cache_dtype: str | None = None
    kv_cache_fraction: float | None = None
    prefix_cache_enabled: bool = False
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


class RecipeUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `artifact` and `version` are absent by design. A recipe is a reproducible way
    to serve some specific weights; repointing it at other weights, or reusing a
    version number for different configuration, makes every deployment that
    names it mean something it did not mean yesterday.
    """

    name: str | UnsetType = UNSET
    engine: str | UnsetType = UNSET
    engine_version: str | UnsetType = UNSET
    engine_image: str | None | UnsetType = UNSET
    engine_args: dict | UnsetType = UNSET
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
