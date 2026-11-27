"""The Kubernetes-shaped manifest of a Workload, Endpoint and Artifact (ADR 0022).

A second representation beside the `/v1` request and read schemas, not a replacement for them.
`metadata` is spelled the Kubernetes way (camelCase) so `kubectl` and Argo can read it later;
`spec` and `status` keep the plane's snake_case vocabulary, so a field reads the same in both.

The JSON Schema carries two markers that apply and diff read:

- `x-immutable`: settable at create only. A later apply that changes it is refused.
- `x-defaulted`: the plane fills it when the manifest leaves it out. A diff compares it only when
  the manifest declared it, so a server default never reads as drift.
"""

import datetime as dt

from typing import Annotated

import msgspec

from msgspec import Meta
from msgspec import field

from app.core.schemas import Struct
from app.modules.artifacts.schemas import ArtifactLocationSchema
from app.modules.artifacts.schemas import ArtifactUpstreamSchema
from app.modules.endpoints.schemas import EndpointSurfaceSchema
from app.modules.workloads.schemas import RoleSchema
from app.modules.workloads.schemas import CacheSchema
from app.modules.workloads.schemas import ShapeSchema
from app.modules.workloads.schemas import RouterSchema
from app.modules.workloads.schemas import OverlaySchema
from app.modules.workloads.schemas import ServingSchema
from app.modules.workloads.schemas import ResourcesSchema
from app.modules.workloads.schemas import ParallelismSchema
from app.modules.workloads.schemas import CapabilitiesSchema
from app.modules.workloads.schemas import TopologyViewSchema


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
    creation_timestamp: dt.datetime | None = field(default=None, name="creationTimestamp")
    resource_version: str | None = field(default=None, name="resourceVersion")


# --------------------------------------------------------------------------- Workload


class RuntimeSpec(Struct, kw_only=True):
    """What process serves the weights. Only `dynamo` today; custom workers add `container`.

    `kind` picks the runtime and the rest is read under it. For `dynamo`, `engine` and `version`
    pick the worker image unless `image` pins one, and `args` are the engine's own flags.
    """

    kind: str = "dynamo"
    engine: str
    version: str
    image: str | None = None
    frontend_image: str | None = None
    args: dict = {}


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

    `allowed_regions` and `allowed_clusters` become the workload's own placement policy at create
    and are not editable through the workload afterwards.
    """

    strategy: str = "pack"
    cluster_floors: dict[str, int] = {}
    max_clusters: int | None = None
    allowed_regions: Annotated[list[str], IMMUTABLE] = []
    allowed_clusters: Annotated[list[str], IMMUTABLE] = []


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


UNSCHEDULABLE = "Unschedulable"


class WorkloadStatus(Struct, kw_only=True):
    """What the plane derived or observed. Never written through a manifest."""

    phase: str
    served_model_name: str
    serving_digest: str
    resolved_engine_args: dict
    imported_args: dict
    topology: TopologyViewSchema
    scale: WorkloadScaleStatus
    deployments: list[WorkloadDeploymentStatus]
    conditions: list[Condition] = []
    artifact_tag: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None


class WorkloadManifest(Struct, kw_only=True):
    api_version: str = field(default=API_VERSION, name="apiVersion")
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

    api_version: str = field(default=API_VERSION, name="apiVersion")
    kind: str = "Scale"
    metadata: ObjectMeta
    spec: ScaleReplicas
    status: ScaleStatus | None = None


# --------------------------------------------------------------------------- Endpoint


class EndpointTargetSpec(Struct, kw_only=True):
    """Today's target shape: a deployment by its object name. P1.6 adds targeting a workload."""

    deployment: str
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


class EndpointStatus(Struct, kw_only=True):
    """The deployments the targets resolved to. Upstream addresses are `/v1/endpoints/{ref}/resolved`."""

    targets: list[EndpointTargetStatus]


class EndpointManifest(Struct, kw_only=True):
    api_version: str = field(default=API_VERSION, name="apiVersion")
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
    api_version: str = field(default=API_VERSION, name="apiVersion")
    kind: str = ARTIFACT
    metadata: ObjectMeta
    spec: ArtifactSpec
    status: ArtifactStatus | None = None


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
