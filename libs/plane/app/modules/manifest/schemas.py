"""The Kubernetes-shaped manifest of a Workload, Endpoint and Artifact (ADR 0022).

A second representation beside the `/v1` request and read schemas, not a replacement for them.
Every field is spelled the Kubernetes way, camelCase, in `metadata`, `spec` and `status` alike
(ADR 0028), so `kubectl` and Argo read it as they would any object. The Python attributes stay
snake_case; msgspec renames on the wire. Free-form maps (`labels`, `args`, `overrides`,
`properties`, engine args) are user data and keep their keys as written.

The JSON Schema carries two markers that apply and diff read:

- `x-immutable`: settable at create only. A later apply that changes it is refused.
- `x-defaulted`: the plane fills it when the manifest leaves it out. A diff compares it only when
  the manifest declared it, so a server default never reads as drift.
"""

import types
import typing
import datetime as dt
import functools

from typing import Literal
from typing import Annotated

import msgspec

from msgspec import Meta
from msgspec import field

from app.core.schemas import Struct as BaseStruct
from app.modules.artifacts.schemas import ArtifactLocationSchema as _ArtifactLocationSchema
from app.modules.artifacts.schemas import ArtifactUpstreamSchema as _ArtifactUpstreamSchema
from app.modules.endpoints.schemas import EndpointSurfaceSchema as _EndpointSurfaceSchema
from app.modules.workloads.schemas import RoleSchema as _RoleSchema
from app.modules.workloads.schemas import CacheSchema as _CacheSchema
from app.modules.workloads.schemas import ShapeSchema as _ShapeSchema
from app.modules.workloads.schemas import EnvVarSchema as _EnvVarSchema
from app.modules.workloads.schemas import RouterSchema as _RouterSchema
from app.modules.workloads.schemas import OverlaySchema as _OverlaySchema
from app.modules.workloads.schemas import ServingSchema as _ServingSchema
from app.modules.workloads.schemas import ResourcesSchema as _ResourcesSchema
from app.modules.workloads.schemas import ParallelismSchema as _ParallelismSchema
from app.modules.workloads.schemas import CapabilitiesSchema as _CapabilitiesSchema
from app.modules.workloads.schemas import TopologyViewSchema as _TopologyViewSchema
from app.modules.workloads.schemas import ContainerPortSchema as _ContainerPortSchema
from app.modules.workloads.schemas import ContainerMetricsSchema as _ContainerMetricsSchema


class Struct(BaseStruct, rename="camel"):
    """A manifest object: camelCase on the wire, snake_case in Python."""


@functools.cache
def camel(cls: type) -> type:
    """A camelCase copy of an HTTP API schema, nested schemas included, for a manifest to embed.

    The HTTP API keeps snake_case, so its schemas are copied, not renamed. `translate`
    converts a read schema's value into the copy with `msgspec.convert(..., from_attributes=True)`.
    """
    hints = typing.get_type_hints(cls, include_extras=True)
    fields = []
    for f in msgspec.structs.fields(cls):
        default = f.default if f.default is not msgspec.NODEFAULT else msgspec.NODEFAULT
        if f.default_factory is not msgspec.NODEFAULT:
            factory = f.default_factory
            default = field(default_factory=lambda factory=factory: camel_value(factory()))
        if default is msgspec.NODEFAULT:
            fields.append((f.name, _camel_type(hints[f.name])))
        else:
            fields.append((f.name, _camel_type(hints[f.name]), default))
    name = cls.__name__.removesuffix("Schema")
    out = msgspec.defstruct(name, fields, bases=(Struct,), module=__name__)
    out.__doc__ = cls.__doc__
    return out


def _camel_type(t):
    if isinstance(t, type) and issubclass(t, msgspec.Struct):
        return camel(t)
    args = typing.get_args(t)
    if not args:
        return t
    origin = typing.get_origin(t)
    if origin is Annotated:
        return Annotated[(_camel_type(args[0]), *t.__metadata__)]
    mapped = tuple(_camel_type(a) for a in args)
    if origin in (typing.Union, types.UnionType):
        return functools.reduce(lambda a, b: a | b, mapped)
    return origin[mapped if len(mapped) > 1 else mapped[0]]


def camel_value(v):
    if isinstance(v, msgspec.Struct):
        return msgspec.convert(v, camel(type(v)), from_attributes=True)
    return v


ArtifactLocationSchema = camel(_ArtifactLocationSchema)
ArtifactUpstreamSchema = camel(_ArtifactUpstreamSchema)
EndpointSurfaceSchema = camel(_EndpointSurfaceSchema)
RoleSchema = camel(_RoleSchema)
CacheSchema = camel(_CacheSchema)
EnvVarSchema = camel(_EnvVarSchema)
ContainerMetricsSchema = camel(_ContainerMetricsSchema)
ContainerPortSchema = camel(_ContainerPortSchema)
ShapeSchema = camel(_ShapeSchema)
RouterSchema = camel(_RouterSchema)
OverlaySchema = camel(_OverlaySchema)
ServingSchema = camel(_ServingSchema)
ResourcesSchema = camel(_ResourcesSchema)
ParallelismSchema = camel(_ParallelismSchema)
CapabilitiesSchema = camel(_CapabilitiesSchema)
TopologyViewSchema = camel(_TopologyViewSchema)


API_VERSION = "plane.wingsmith.dev/v1alpha1"
"""Versioned apart from the HTTP API's `/v1`: a manifest shape carries a conversion obligation the
HTTP API does not. `wingsmith.dev` is the operator's CRD group and is not reused here."""

WORKLOAD = "Workload"
ENDPOINT = "Endpoint"
ARTIFACT = "Artifact"

NOTE_ANNOTATION = "plane.wingsmith.dev/note"
"""The workload's latest revision note. A note lands on a revision, not the workload: not spec."""

IMMUTABLE = Meta(extra_json_schema={"x-immutable": True})
DEFAULTED = Meta(extra_json_schema={"x-defaulted": True})
IMMUTABLE_DEFAULTED = Meta(extra_json_schema={"x-immutable": True, "x-defaulted": True})


class ObjectMeta(Struct, kw_only=True):
    """Identity. `namespace` is the project's slug, and is absent on a public Artifact.

    `uid`, `generation`, `creationTimestamp` and `resourceVersion` are read-only: the plane writes
    them and ignores them on input. `resourceVersion` is the object's `revision`, the value
    `If-Match` takes.
    """

    name: str
    namespace: str | None = None
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    uid: str | None = None
    generation: int | None = None
    creation_timestamp: dt.datetime | None = None
    resource_version: str | None = None


# --------------------------------------------------------------------------- Workload


ContainerProtocol = Literal[
    "openai-chat", "openai-images", "openai-audio-speech", "openai-audio-transcriptions", "openai-videos"
]
"""`app.core.serving.CONTAINER_PROTOCOLS` spelled as a type, so the published manifest schema lists them."""


class RuntimeSpec(Struct, kw_only=True, omit_defaults=True):
    """What process serves the weights: a tagged union on `kind`, `dynamo` when left out.

    `dynamo`: `engine` and `version` pick the worker image unless `image` pins one, and `args`
    is a map of the engine's own flags.

    `container` (a custom worker): `image` is pinned by digest, `command` and `args` are lists
    (`args` may use `$(WS_MODEL_PATH)`, `$(WS_SERVED_MODEL_NAME)`, `$(WS_PORT)`), `ports` names
    one `http`, `protocol` is one OpenAI API family (`openai-chat` by default, or `openai-images`,
    `openai-audio-speech`, `openai-audio-transcriptions`, `openai-videos`) and implies the output
    modality, the probes are `corev1.Probe` objects, and
    `metrics` is `{port, path}`. `engine`, `version` and `frontendImage` are not read.

    Unset fields are left out of a read, so each kind shows only its own.
    """

    kind: Annotated[str | None, IMMUTABLE_DEFAULTED] = None
    engine: str | None = None
    version: str | None = None
    image: str | None = None
    frontend_image: str | None = None
    args: dict | list[str] = {}
    command: list[str] = []
    env: list[EnvVarSchema] = []
    ports: list[ContainerPortSchema] = []
    protocol: ContainerProtocol | None = None
    readiness_probe: dict | None = None
    liveness_probe: dict | None = None
    startup_probe: dict | None = None
    metrics: ContainerMetricsSchema | None = None
    preset: str | None = None
    """`vllm` or `sglang` under `kind: container`: the plane fills image, command, serve args,
    port, probes and metrics, `version` picks the engine release, and `args` are appended.
    `status.runtime` shows the expansion."""


class WorkloadServingSpec(Struct, kw_only=True):
    """How the weights are served, independent of which runtime runs them.

    `artifact` and `adapters` are artifact handles, resolved in the manifest's project, private
    first, then public. `api` is the served API's behaviour (`serving` on the HTTP API).
    """

    artifact: str
    adapters: list[str] = []
    shape: ShapeSchema
    context_length: int = 0
    max_batch_tokens: int | None = None
    quantization: str | None = None
    dtype: str | None = None
    parallelism: ParallelismSchema = field(default_factory=ParallelismSchema)
    cache: CacheSchema = field(default_factory=CacheSchema)
    api: ServingSchema = field(default_factory=ServingSchema)
    capabilities: CapabilitiesSchema = field(default_factory=CapabilitiesSchema)
    resources: ResourcesSchema = field(default_factory=ResourcesSchema)
    overlay: OverlaySchema = field(default_factory=OverlaySchema)
    topology: str = "aggregated"
    roles: list[RoleSchema] = []
    router: RouterSchema = field(default_factory=RouterSchema)


class ScaleSpec(Struct, kw_only=True):
    """The bounds `replicas` moves within. `desired` is a read-only alias of `spec.replicas`,
    kept in v1alpha1 and dropped before beta (ADR 0025): an apply never reads it."""

    min: int = 1
    max: int = 1
    desired: int = 1


class PlacementSpec(Struct, kw_only=True):
    """Where replicas may go. Cluster references are names.

    `allowed_regions` and `allowed_clusters` become the workload's own placement policy.
    `allowed_regions` is fixed at create; `allowed_clusters` may change and takes effect at the next
    expansion (ADR 0042).
    """

    strategy: str = "pack"
    cluster_floors: dict[str, int] = {}
    max_clusters: int | None = None
    allowed_regions: Annotated[list[str], IMMUTABLE] = []
    allowed_clusters: list[str] = []


class WorkloadManifestSpec(Struct, kw_only=True):
    """What the workload declares.

    `enabled` is create-only and afterwards lives on each deployment, so a read leaves it null.
    """

    kind: Annotated[str, IMMUTABLE] = "service"
    gang_size: Annotated[int, IMMUTABLE] = 1
    cluster: Annotated[str | None, IMMUTABLE] = None
    enabled: Annotated[bool | None, IMMUTABLE] = None
    priority_class: Annotated[str | None, DEFAULTED] = None
    preemptible: bool = False
    paused: bool = False
    deadline_at: dt.datetime | None = None
    replicas: Annotated[int | None, DEFAULTED] = None
    scale: ScaleSpec = field(default_factory=ScaleSpec)
    placement: PlacementSpec = field(default_factory=PlacementSpec)
    runtime: RuntimeSpec
    serving: WorkloadServingSpec
    overrides: dict = {}


class WorkloadDeploymentStatus(Struct, kw_only=True):
    name: str
    cluster: str
    admission_phase: str
    authority: str
    enabled: bool
    desired_replicas: int
    ready_replicas: int


class WorkloadScaleStatus(Struct, kw_only=True):
    placed: int
    ready: int


class Condition(Struct, kw_only=True):
    """A Kubernetes-style condition: `status` is `"True"`, `"False"` or `"Unknown"`."""

    type: str
    status: str
    reason: str
    message: str = ""
    last_transition_time: dt.datetime | None = None
    """Null where the plane derives the condition on read and keeps no history of it."""


UNSCHEDULABLE = "Unschedulable"
PLACEMENT_DRIFT = "PlacementDrift"
READY = "Ready"


class WorkloadStatus(Struct, kw_only=True):
    """What the plane derived or observed. Never written through a manifest."""

    phase: str
    served_model_name: str
    serving_digest: str
    resolved_engine_args: dict
    imported_args: dict
    topology: TopologyViewSchema
    runtime: RuntimeSpec | None = None
    """The container worker a `preset` expands to, as it runs. Absent without a preset."""
    scale: WorkloadScaleStatus
    deployments: list[WorkloadDeploymentStatus]
    conditions: list[Condition] = []
    artifact_tag: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None


class WorkloadManifest(Struct, kw_only=True):
    api_version: str = API_VERSION
    kind: str = WORKLOAD
    metadata: ObjectMeta
    spec: WorkloadManifestSpec
    status: WorkloadStatus | None = None


class ScaleReplicas(Struct, kw_only=True):
    replicas: int


class ScaleStatus(Struct, kw_only=True):
    """`replicas` is what placement placed, which is below `spec.replicas` while unschedulable."""

    replicas: int
    ready_replicas: int = 0


class Scale(Struct, kw_only=True):
    """The `/scale` subresource: a workload's replica count alone, Kubernetes `autoscaling/v1`
    shaped. `metadata.resourceVersion` on a PUT counts as `If-Match`."""

    api_version: str = API_VERSION
    kind: str = "Scale"
    metadata: ObjectMeta
    spec: ScaleReplicas
    status: ScaleStatus | None = None


# --------------------------------------------------------------------------- Endpoint


class WorkloadRef(Struct, kw_only=True):
    """A workload by name in the endpoint's own namespace, like a same-namespace object ref."""

    name: str


class EndpointTargetSpec(Struct, kw_only=True):
    """A deployment by its object name, or a workload by `workload_ref` (ADR 0026): exactly one.
    A workload ref routes to that workload's current deployments, its weight split evenly over
    them, and may name a workload that does not exist yet."""

    deployment: str | None = None
    workload_ref: WorkloadRef | None = None
    weight: int = 100
    role: str = "primary"
    enabled: bool = True
    served_model_name: str | None = None


class EndpointSpec(Struct, kw_only=True):
    slug: Annotated[str, DEFAULTED] = ""
    visibility: str = "private"
    routing_strategy: str = "weighted"
    enabled: bool = True
    aliases: list[str] = []
    targets: list[EndpointTargetSpec] = []
    failover_order: list[str] = []
    surface: EndpointSurfaceSchema = field(default_factory=EndpointSurfaceSchema)


class EndpointTargetStatus(Struct, kw_only=True):
    deployment: str
    deployment_id: str
    workload: str | None = None
    """Set when the deployment came from a workload ref."""


class EndpointStatus(Struct, kw_only=True):
    """The deployments the targets resolved to. Upstream addresses are `/v1/endpoints/{ref}/resolved`.

    `ResolvedRefs` is `False` while a workload ref names no workload in the namespace."""

    targets: list[EndpointTargetStatus]
    conditions: list[Condition] = []


class EndpointManifest(Struct, kw_only=True):
    api_version: str = API_VERSION
    kind: str = ENDPOINT
    metadata: ObjectMeta
    spec: EndpointSpec
    status: EndpointStatus | None = None


# --------------------------------------------------------------------------- Artifact


class ArtifactSpec(Struct, kw_only=True):
    """The weights, by content. `metadata.name` is the handle.

    `digest` and `source` are create-only. `source` (`hf://org/repo@revision`) resolves to the digest
    and the fields marked defaulted at create, and is not stored, so a read leaves it null.
    `project` is attribution (who registered it), not scope: scope is `metadata.namespace`.
    `properties` is the HTTP API's free-form `metadata` map.
    """

    digest: Annotated[str, IMMUTABLE] = ""
    source: Annotated[str | None, IMMUTABLE] = None
    display_name: Annotated[str, DEFAULTED] = ""
    slug: Annotated[str, DEFAULTED] = ""
    kind: str = "weights"
    format: Annotated[str | None, DEFAULTED] = None
    size_bytes: Annotated[int, DEFAULTED] = 0
    licence: Annotated[str | None, DEFAULTED] = None
    parameter_count: Annotated[int | None, DEFAULTED] = None
    base_artifact: str | None = None
    project: str | None = None
    properties: dict = {}
    locations: list[ArtifactLocationSchema] = []
    upstream: ArtifactUpstreamSchema = field(default_factory=ArtifactUpstreamSchema)


class ArtifactStatus(Struct, kw_only=True):
    """`residency` is the regions a location names. Fitment is `/v1/artifacts/{ref}/fitment`: it is
    a query over the fleet with parameters, not a property of the artifact."""

    verified: bool
    residency: list[str]


class ArtifactManifest(Struct, kw_only=True):
    api_version: str = API_VERSION
    kind: str = ARTIFACT
    metadata: ObjectMeta
    spec: ArtifactSpec
    status: ArtifactStatus | None = None


class ManifestListMeta(Struct, kw_only=True):
    """`resourceVersion` is the change-log seq every item is current as of: watch from it."""

    resource_version: str


class WorkloadManifestList(Struct, kw_only=True):
    api_version: str = API_VERSION
    kind: str = "WorkloadList"
    metadata: ManifestListMeta
    items: list[WorkloadManifest]


class EndpointManifestList(Struct, kw_only=True):
    api_version: str = API_VERSION
    kind: str = "EndpointList"
    metadata: ManifestListMeta
    items: list[EndpointManifest]


class ArtifactManifestList(Struct, kw_only=True):
    api_version: str = API_VERSION
    kind: str = "ArtifactList"
    metadata: ManifestListMeta
    items: list[ArtifactManifest]


MANIFESTS = {WORKLOAD: WorkloadManifest, ENDPOINT: EndpointManifest, ARTIFACT: ArtifactManifest}


def json_schema() -> dict:
    """The three kinds as one JSON Schema document, `x-immutable` and `x-defaulted` included.

    Built by msgspec: the OpenAPI document drops the markers.
    """
    refs, components = msgspec.json.schema_components(MANIFESTS.values(), ref_template="#/$defs/{name}")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": API_VERSION,
        "oneOf": list(refs),
        "$defs": {k: components[k] for k in sorted(components)},
    }
