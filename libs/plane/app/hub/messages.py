"""Wire types for the plane <-> operator protocol.

The envelope and its semantics are specified in `contract/PROTOCOL.md`; the type registry is
`contract/MESSAGES.md`. These Structs are hand-written by design (see ADR 0001) and are kept
honest against the Go side by the golden corpus in `contract/fixtures/`.

Timestamps are RFC 3339 UTC with milliseconds and are carried as `str`, not `datetime`. The wire
form is the contract, and a datetime round-trip is lossy with respect to its own text: msgspec
re-encodes to microsecond precision, Go's time.Time to nanoseconds, so neither reproduces the
canonical document it decoded. `TIMESTAMP_PATTERN` is the validation seam.
"""

import re

from typing import Literal

from msgspec import Raw
from msgspec import Struct
from msgspec import json as msgjson


ENVELOPE_VERSION = 1

TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

ID_PREFIXES = (
    "clu_",
    "nod_",
    "dev_",
    "tpd_",
    "pol_",
    "art_",
    "rec_",
    "dep_",
    "rep_",
    "end_",
    "prj_",
    "qta_",
    "opr_",
    "cmp_",
    "dsw_",
)

UNSEQUENCED_TYPES = frozenset({"hello", "welcome", "ping", "pong", "ack"})


class Envelope[T](Struct):
    """One frame. Every WebSocket text message is exactly one JSON-encoded Envelope."""

    v: int
    type: str
    id: str
    ts: str
    payload: T
    seq: int | None = None
    ack: int | None = None
    reply_to: str | None = None

    @property
    def sequenced(self) -> bool:
        """Whether this frame participates in resume and enters a replay buffer."""
        return self.type not in UNSEQUENCED_TYPES


RawEnvelope = Envelope[Raw]


# --------------------------------------------------------------------------- session


class Hello(Struct):
    """The operator's first frame. Never sent twice on one connection.

    `lossy` is how a sender whose replay buffer overflowed asks for a fresh session instead of
    pretending it can resume. `session_id` names which session `resume_from` counts within — a
    sequence number alone is meaningless across sessions.
    """

    cluster_id: str
    agent_version: str
    lossy: bool
    resume_from: int | None = None
    session_id: str | None = None


class Welcome(Struct):
    session_id: str
    ack_seq: int
    full_resync: bool


class Ping(Struct):
    pass


class Pong(Struct):
    pass


class Ack(Struct):
    """Carries acknowledgement in the envelope's `ack` field when there is nothing else to send.

    The payload is empty by definition: the acknowledgement is the frame.
    """


ErrorCode = Literal["not_found", "invalid", "conflict", "unavailable", "internal", "deadline"]


class Error(Struct):
    code: ErrorCode
    message: str
    retryable: bool


class StreamEnd(Struct):
    """Terminates a stream sharing one `reply_to`, and closes a resync.

    `complete` is load-bearing: a receiver prunes local state against a snapshot only when the
    stream ended with `complete` true. A truncated snapshot must leave local state untouched
    rather than be read as an authoritative empty set.
    """

    count: int
    complete: bool


# --------------------------------------------------------------------------- resync


class ResyncBegin(Struct):
    """Opens a full-resync stream. It is closed by `StreamEnd`; there is no `resync.end`."""

    scope: Literal["supply", "demand", "all"]


# --------------------------------------------------------------------------- supply


class NodeCondition(Struct):
    type: str
    status: Literal["true", "false", "unknown"]
    reason: str
    message: str
    since: str


class NodeObserved(Struct):
    node_id: str
    cluster_id: str
    name: str
    pool: str
    membership: Literal["joined", "leaving", "left"]
    ready: bool
    cordoned: bool
    conditions: list[NodeCondition]
    kernel_version: str
    driver_version: str
    runtime_version: str
    agent_version: str
    device_ids: list[str]
    labels: dict[str, str]
    observed_at: str


class NodeEvent(Struct):
    node_id: str
    cluster_id: str
    event: Literal["joined", "left", "cordoned", "uncordoned", "degraded"]
    reason: str
    message: str
    occurred_at: str


class DeviceEcc(Struct):
    mode_enabled: bool
    volatile_corrected: int
    volatile_uncorrected: int
    aggregate_uncorrected: int


class DeviceMig(Struct):
    enabled: bool
    profile: str | None = None
    instance_id: str | None = None


class DeviceAllocation(Struct):
    replica_id: str
    deployment_id: str
    project_id: str


class DeviceObserved(Struct):
    device_id: str
    node_id: str
    cluster_id: str
    uuid: str
    index: int
    model: str
    device_class: str
    memory_total_mb: int
    health: Literal["healthy", "degraded", "unhealthy", "unknown"]
    ecc: DeviceEcc
    throttled: bool
    throttle_reasons: list[str]
    mig: DeviceMig
    observed_at: str
    topology_domain_id: str | None = None
    allocation: DeviceAllocation | None = None


class DeviceEvent(Struct):
    device_id: str
    node_id: str
    cluster_id: str
    event: Literal["degraded", "recovered", "reset", "fell_off_bus"]
    reason: str
    message: str
    occurred_at: str


class TopologyObserved(Struct):
    """One topology domain, the devices that share it, and the nodes it spans.

    `node_ids` is a list because a domain is not a property of a machine: an
    NVSwitch fabric or a GB200 rack is one NVLink domain across several nodes.
    Zero, one, or many are all legal, and `spans_nodes` is the cluster's own
    answer rather than something the plane recomputes.
    """

    domain_id: str
    cluster_id: str
    kind: Literal["nvlink", "pcie_root", "numa"]
    node_ids: list[str]
    spans_nodes: bool
    device_ids: list[str]
    observed_at: str


class WorkloadDiscovered(Struct):
    """A workload already running in a cluster that the plane never declared.

    Purely observed, and deliberately not a `Deployment`: the plane has declared
    nothing about this object, so giving it a declared record would immediately
    create a difference between what we say and what is running — and the first
    reconcile of that difference is a rollout of weights that are already warm.
    It stays a sighting until someone decides otherwise.

    `devices_per_replica` and `devices_total` are the cluster's own accounting,
    not a measurement, which is what keeps this on the sync stream rather than in
    the telemetry pipeline.

    `kind` is open text where every other enum on the wire is closed. A cluster is
    entitled to run models under something we have never heard of, and a closed
    set would make that workload undecodable — dropped rather than reported,
    which is the one outcome an inventory must not have.
    """

    workload_id: str
    cluster_id: str
    namespace: str
    name: str
    kind: str
    images: list[str]
    replicas_desired: int
    replicas_ready: int
    devices_per_replica: int
    devices_total: int
    node_ids: list[str]
    labels: dict[str, str]
    spec_fingerprint: str
    observed_at: str
    model: str | None = None


class CapacityClass(Struct):
    device_class: str
    total: int
    free: int
    allocated: int
    unhealthy: int


class CapacityObserved(Struct):
    cluster_id: str
    pool: str
    classes: list[CapacityClass]
    observed_at: str


# --------------------------------------------------------------------------- demand, hub -> op


class RecipeParallelism(Struct):
    tensor: int
    pipeline: int
    expert: int


class RecipeResources(Struct):
    cpu_cores: int | None = None
    memory_mib: int | None = None
    disk_gib: int | None = None


class RecipeDesired(Struct):
    """How to serve an artifact, in enough detail for a cluster to render it.

    A declared object of its own rather than a block inlined on every deployment
    that names it. Recipes are shared — several deployments of the same model on
    the same cluster differ in scale and placement, not in engine configuration —
    so inlining would ship the same body repeatedly and turn one recipe edit into
    a rewrite of every frame referencing it.

    The device shape is deliberately absent. It reaches the cluster on the
    deployment's `placement`, derived from this recipe, and carrying it twice
    invites the two copies to disagree.
    """

    recipe_id: str
    cluster_id: str
    artifact_id: str
    name: str
    version: int
    engine: str
    engine_version: str
    engine_image: str | None
    engine_args: dict[str, str]
    quantization: str | None
    dtype: str | None
    parallelism: RecipeParallelism
    context_length: int
    max_batch_tokens: int | None
    kv_cache_dtype: str | None
    kv_cache_fraction: float | None
    prefix_cache_enabled: bool
    prefix_cache_config: dict[str, str]
    resources: RecipeResources
    labels: dict[str, str]
    updated_at: str


class PlacementConstraints(Struct):
    device_class: str
    devices_per_replica: int
    min_device_memory_mb: int
    topology: Literal["none", "single_node", "single_domain"]
    allowed_pools: list[str]
    node_selector: dict[str, str]


class Adopt(Struct):
    """Rides on a `deployment.desired` that was promoted from a discovered workload.

    Its presence changes what the frame asks for. The named object already exists
    and is already serving, so the operator takes ownership of it in place rather
    than applying a rendered spec over it.

    `spec_fingerprint` is what was true when the promotion was accepted. A
    reconciler finding the live object digesting to anything else must refuse and
    say so: the workload drifted, or our render disagrees with reality, and either
    way converging means restarting a model to close a gap nobody asked to close.
    """

    namespace: str
    kind: str
    name: str
    spec_fingerprint: str


class DeploymentDesired(Struct):
    deployment_id: str
    cluster_id: str
    project_id: str
    recipe_id: str
    recipe_revision: int
    lifecycle: Literal["service", "task", "gang"]
    priority_class: str
    min_replicas: int
    max_replicas: int
    placement: PlacementConstraints
    artifact_ids: list[str]
    generation: int
    updated_at: str
    adopt: Adopt | None = None


class DeploymentDeleted(Struct):
    deployment_id: str
    cluster_id: str
    generation: int
    deleted_at: str


class ArtifactSource(Struct):
    kind: Literal["s3", "https", "oci", "hf"]
    uri: str
    priority: int
    credential_ref: str | None = None


class ArtifactDesired(Struct):
    artifact_id: str
    cluster_id: str
    kind: Literal["weights", "adapter", "dataset", "image"]
    digest: str
    format: str
    size_bytes: int
    licence: str
    sources: list[ArtifactSource]
    updated_at: str


class ComponentDesired(Struct):
    component_id: str
    cluster_id: str
    name: str
    version: str
    enabled: bool
    config: dict[str, str]
    updated_at: str


class CacheWarmEntry(Struct):
    artifact_id: str
    priority: int
    pin: bool


class CacheDesired(Struct):
    cluster_id: str
    entries: list[CacheWarmEntry]
    eviction_policy: Literal["lru", "lfu", "none"]
    capacity_bytes: int
    updated_at: str


# --------------------------------------------------------------------------- demand, op -> hub


class LastError(Struct):
    code: str
    message: str
    at: str


class DeploymentObserved(Struct):
    deployment_id: str
    cluster_id: str
    generation: int
    phase: Literal["pending", "admitted", "placed", "running", "degraded", "failed", "terminating"]
    desired_replicas: int
    ready_replicas: int
    updated_replicas: int
    unavailable_replicas: int
    observed_at: str
    last_error: LastError | None = None


class ReplicaObserved(Struct):
    replica_id: str
    name: str
    deployment_id: str
    cluster_id: str
    node_id: str
    device_ids: list[str]
    phase: Literal["pending", "starting", "ready", "draining", "terminated", "failed"]
    ready: bool
    engine: str
    engine_version: str
    recipe_id: str
    restart_count: int
    observed_generation: int
    observed_at: str
    started_at: str | None = None


class ReplicaEvent(Struct):
    replica_id: str
    deployment_id: str
    cluster_id: str
    event: Literal["started", "ready", "failed", "oom", "preempted"]
    reason: str
    message: str
    occurred_at: str


class AllocationObserved(Struct):
    allocation_id: str
    cluster_id: str
    replica_id: str
    deployment_id: str
    project_id: str
    node_id: str
    device_ids: list[str]
    exclusive: bool
    granted_at: str
    observed_at: str


class CacheNodeResidency(Struct):
    """What one node holds of one artifact.

    A node mid-fetch and a node already serving are not the same fact, and a
    router choosing where to send a request needs to tell them apart — so state
    and bytes are per node here rather than rolled up. The cluster-level numbers
    on `CacheObserved` stay, because a tier that is not a local disk has
    residency with no node to attribute it to.
    """

    node_id: str
    state: Literal["absent", "fetching", "resident", "evicting"]
    bytes_resident: int
    path: str | None = None


class CacheObserved(Struct):
    cluster_id: str
    artifact_id: str
    state: Literal["absent", "fetching", "resident", "evicting"]
    resident: bool
    bytes_resident: int
    nodes: list[CacheNodeResidency]
    eviction_pressure: float
    observed_at: str
    last_accessed_at: str | None = None


class ComponentObserved(Struct):
    component_id: str
    cluster_id: str
    name: str
    desired_version: str
    running_version: str
    phase: Literal["pending", "rolling", "healthy", "degraded", "failed"]
    ready_instances: int
    total_instances: int
    observed_at: str
    last_error: LastError | None = None


# --------------------------------------------------------------------------- operations


class OperationTarget(Struct):
    kind: Literal["cluster", "node", "device", "deployment", "component", "artifact"]
    id: str


class OperationDesired(Struct):
    operation_id: str
    cluster_id: str
    kind: Literal["drain", "cordon", "reboot", "device_reset", "cache_warm", "roll", "upgrade"]
    target: OperationTarget
    reason: str
    requested_by: str
    deadline: str
    parameters: dict[str, str]
    created_at: str


class OperationFailure(Struct):
    code: str
    message: str
    retryable: bool


class OperationObserved(Struct):
    operation_id: str
    cluster_id: str
    phase: Literal["pending", "running", "succeeded", "failed", "cancelled", "timed_out"]
    progress: float
    message: str
    observed_at: str
    result: dict[str, str] | None = None
    failure: OperationFailure | None = None
    started_at: str | None = None
    completed_at: str | None = None


MESSAGE_TYPES: dict[str, type[Struct]] = {
    "hello": Hello,
    "welcome": Welcome,
    "ping": Ping,
    "pong": Pong,
    "error": Error,
    "ack": Ack,
    "stream.end": StreamEnd,
    "resync.begin": ResyncBegin,
    "node.observed": NodeObserved,
    "node.event": NodeEvent,
    "device.observed": DeviceObserved,
    "device.event": DeviceEvent,
    "topology.observed": TopologyObserved,
    "capacity.observed": CapacityObserved,
    "workload.discovered": WorkloadDiscovered,
    "deployment.desired": DeploymentDesired,
    "deployment.deleted": DeploymentDeleted,
    "recipe.desired": RecipeDesired,
    "artifact.desired": ArtifactDesired,
    "component.desired": ComponentDesired,
    "cache.desired": CacheDesired,
    "deployment.observed": DeploymentObserved,
    "replica.observed": ReplicaObserved,
    "replica.event": ReplicaEvent,
    "allocation.observed": AllocationObserved,
    "cache.observed": CacheObserved,
    "component.observed": ComponentObserved,
    "operation.desired": OperationDesired,
    "operation.observed": OperationObserved,
}


def decode(data: bytes) -> Envelope[Struct]:
    """Decode a frame into an Envelope whose payload is the Struct registered for its type.

    Raises:
        KeyError: the `type` is not in `MESSAGE_TYPES`. Callers on the hot path log and drop
            rather than propagate, per PROTOCOL §2.
    """
    raw = msgjson.decode(data, type=RawEnvelope)
    payload = msgjson.decode(raw.payload, type=MESSAGE_TYPES[raw.type])
    return Envelope(
        v=raw.v,
        type=raw.type,
        id=raw.id,
        ts=raw.ts,
        payload=payload,
        seq=raw.seq,
        ack=raw.ack,
        reply_to=raw.reply_to,
    )


def encode(envelope: Envelope[Struct]) -> bytes:
    """Encode a frame to its JSON wire form."""
    return msgjson.encode(envelope)
