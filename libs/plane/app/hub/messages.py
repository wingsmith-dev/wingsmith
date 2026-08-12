"""Wire types for the plane <-> operator protocol.

The envelope and its semantics are specified in `libs/packages/contract/PROTOCOL.md`; the type registry is
`libs/packages/contract/MESSAGES.md`. These Structs are hand-written by design (see ADR 0001) and are kept
honest against the Go side by the golden corpus in `libs/packages/contract/fixtures/`.

Timestamps are RFC 3339 UTC with milliseconds and are carried as `str`, not `datetime`. The wire
form is the contract, and a datetime round-trip is lossy with respect to its own text: msgspec
re-encodes to microsecond precision, Go's time.Time to nanoseconds, so neither reproduces the
canonical document it decoded. `TIMESTAMP_PATTERN` is the validation seam.
"""

import re

from typing import Any
from typing import Literal

from msgspec import Raw
from msgspec import Struct
from msgspec import json as msgjson
from msgspec import field


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
    operator_version: str
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


class ClusterObserved(Struct):
    """What a cluster turned out to be, as opposed to what it was declared to be.

    `distribution` is the only orchestrator name anywhere in the plane, and it is
    stored and displayed rather than read: the moment something branches on it,
    the plane knows what Kubernetes is and rule 2 is gone.

    `read_only` is about the operator rather than the cluster, and it is the one
    field here that changes what an answer from this cluster means. A read-only
    operator runs without its reconcile loop: it reports everything and applies
    nothing. Without it on the wire the plane cannot tell an operator that
    declines to act from one that has not acted yet, and a workload placed there
    sits at zero replicas with no error to explain it. It defaults false so an
    operator too old to send it reads as a normal one, which is what it is.

    `multinode` says a leader-worker backend is installed, so this cluster can run
    a replica whose devices do not fit on one machine. It is the one observed field
    here that placement reads rather than merely displays: sending a replica that
    spans nodes to a cluster without a backend renders a single pod holding a
    fraction of the devices the engine expects, which hangs in collective
    initialisation instead of failing. It defaults false because an operator too
    old to send it is one that could not have detected a backend either.
    """

    cluster_id: str
    nodes: int
    devices: int
    observed_at: str
    distribution: str = ""
    cloud: str = ""
    region: str = ""
    read_only: bool = False
    multinode: bool = False


class NodeCondition(Struct):
    type: str
    status: Literal["true", "false", "unknown"]
    reason: str
    message: str
    since: str


class NodeTaint(Struct):
    key: str
    effect: str
    value: str = ""


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
    """The node's container runtime — `containerd://1.7.20`, off the kubelet.

    A machine's runtime, not a workload's: `ServingSpec.engine_runtime_image` is the
    Dynamo build the engine runs in, and this is what would start a container of it.
    """
    operator_version: str
    device_ids: list[str]
    labels: dict[str, str]
    observed_at: str
    instance_type: str = ""
    cloud: str = ""
    zone: str = ""
    region: str = ""
    cpu_cores: int = 0
    memory_mib: int = 0
    cpu_arch: str = ""
    os_image: str = ""
    taints: list[NodeTaint] | None = None


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


class WorkloadSpec(Struct):
    """How a discovered workload was told to serve, verbatim from its own cluster.

    Uninterpreted on purpose. `args` arrives as the list the container was given
    and stays that way through ingestion; reading `--tensor-parallel-size` as
    parallelism happens in `modules/discovered/derive.py`, so that improving the
    reading is a plane deploy rather than an operator roll across the fleet.

    `env_names` carries names and never values. A serving pod is routinely handed
    a HuggingFace token or a registry credential through its environment, and a
    discovery pass that shipped values would copy every one of them into this
    database and into its backups.
    """

    image: str | None = None
    command: list[str] = []
    args: list[str] = []
    env_names: list[str] = []
    requests: dict[str, str] = {}
    limits: dict[str, str] = {}
    node_selector: dict[str, str] = {}


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
    spec: WorkloadSpec | None = None


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


class Overlay(Struct):
    """Escape hatches for what this plane does not model.

    Engines and their operators carry far more configuration than any control
    plane wants to mirror field by field, and mirroring it is a losing race —
    every vLLM release adds flags. So the plane models what it reasons about
    (shape, parallelism, context, cache) and passes the rest through.

    `pod_spec` is merged over the rendered pod with Kubernetes strategic-merge
    semantics, so containers, env, volumes and volume mounts merge by name and
    adding one environment variable does not mean restating the others.

    `dynamo` is merged over the rendered graph deployment. There is no schema to
    read merge keys from there, so maps merge and lists replace — which is the
    intended way to hand-write a topology the renderer cannot derive.
    """

    pod_spec: dict = {}
    dynamo: dict = {}


class Parallelism(Struct):
    tensor: int
    pipeline: int
    expert: int


class Resources(Struct):
    cpu_cores: int | None = None
    memory_mib: int | None = None
    disk_gib: int | None = None


class RoleSpec(Struct):
    """One role of a disaggregated deployment: what makes its process different, and no counts.

    The split between this and `DeploymentDesired.role_replicas` is `serving_digest`'s own split
    made structural. What is here decides what the process *is* — prefill runs
    `--max-num-batched-tokens` 32768 against decode's 2048 — and is inside the digest. How many of
    each there are is scale, travels beside `min_replicas`, and is deliberately outside it.
    """

    role: Literal["prefill", "decode"]
    max_batch_tokens: int | None = None
    engine_args: dict[str, str] = {}


class RouterSpec(Struct, omit_defaults=True):
    """How a deployment's own frontend routes between its workers. Every field optional.

    Each field is one `DYN_ROUTER_*` variable on the frontend, and an absent field renders no
    variable, so Dynamo's own default stands — except `mode`, whose absence the operator renders as
    `kv`, which is what it rendered before this block existed. A typed block rather than an
    environment passthrough: the plane validates every value against what Dynamo accepts before it
    is sent.
    """

    mode: Literal["kv", "round-robin", "random", "power-of-two", "least-loaded", "device-aware-weighted"] | None = None
    queue_threshold: float | None = None
    queue_policy: Literal["fcfs", "wspt"] | None = None
    kv_overlap_score_credit: float | None = None


class DiffusionSpec(Struct):
    """What a vLLM-Omni worker reads from its stage-config file. The operator writes the file."""

    model_class_name: str | None = None
    default_sampling_params: dict[str, Any] = {}


class ServingSpec(Struct):
    """How to serve an artifact, in enough detail for a cluster to render it.

    Inlined on the deployment that asks for it rather than travelling as a
    declared object of its own. It used to be `recipe.desired`, on the argument
    that recipes are shared and inlining would ship one body repeatedly. The
    argument did not survive contact with the sender: recipes were filtered and
    serialised per cluster, so a configuration used in three clusters was three
    frames either way, and the only case that ever saved a byte was two
    deployments in ONE cluster naming one recipe. What the separate frame did buy
    was an ordering rule, a `RecipeNotDeclared` pending reason, and a resume race
    in which a deployment waited forever for a recipe it was never sent. Inlined,
    a deployment is renderable the moment its artifact has landed.

    The device shape is deliberately absent, and that rule is unchanged. It
    reaches the cluster on the deployment's `placement`, derived from these same
    fields; two copies of one fact on one wire is two chances to disagree.

    `frontend_image` is a second image and not a spelling of `engine_runtime_image`, because a
    frontend and a worker are separately versioned binaries. It is empty in the ordinary
    case, and empty means the cluster decides — never "use the engine image", which is a
    default only the operator is entitled to pick.

    `digest` is the `serving_digest` of the workload this came from. The cluster does
    not act on it; it is carried so that a replica, a benchmark run and a frame all
    name the configuration the same way.
    """

    engine: str
    """Which engine the worker launches inside the runtime — a module and a flag dialect."""
    engine_runtime_version: str
    """A tag of the Dynamo runtime the engine runs in — `1.4.1` — never the engine's version.

    The two count in different sequences: the vLLM inside `vllm-runtime:1.4.1` is on
    `0.28.0`. The engine's own version is on no frame at all — which build NVIDIA
    compiled in is a property of that image, and the cluster does not report it.
    """
    engine_runtime_image: str | None
    frontend_image: str | None
    engine_args: dict[str, str]
    quantization: str | None
    dtype: str | None
    parallelism: Parallelism
    context_length: int
    max_batch_tokens: int | None
    kv_cache_dtype: str | None
    kv_cache_fraction: float | None
    prefix_cache_enabled: bool
    prefix_cache_config: dict[str, str]
    resources: Resources
    digest: str
    overlay: Overlay = field(default_factory=Overlay)
    engine_version: str = ""
    """The same value as `engine_runtime_version`, under the name an older operator reads.

    A cluster picks up a new operator on its own `helm upgrade`, so a new plane talks to
    operators on both sides of the rename. One that read only the new keys would find
    nothing, render no prefetch job, and leave the pods waiting on a download nobody
    started — a silent stall rather than a rejected frame. Written by `serving_spec` and
    read by nothing here. Delete both once no operator in the fleet is that old.
    """
    engine_image: str | None = None
    """The same value as `engine_runtime_image`, under the name an older operator reads."""

    topology: Literal["aggregated", "disaggregated"] = "aggregated"
    """Whether prefill and decode are one process or two exchanging a KV cache.

    Defaulted rather than optional so an operator too old to read it is unaffected, and so an
    aggregated deployment's frame is byte-identical to what it was.
    """

    roles: list[RoleSpec] = []
    """Per-role configuration, empty on an aggregated deployment.

    Empty is the only way to say aggregated. A single-entry list meaning "one worker" would make
    every existing deployment a disaggregated one with a degenerate ratio, and the render has to
    stay byte-identical for those.
    """

    router: RouterSpec | None = None
    """The frontend's router settings, or None when the workload declares none.

    None renders exactly what a deployment rendered before the block existed. A block is only sent
    when at least one field is set, so "declared nothing" has one spelling on the wire.
    """

    output_modality: Literal["image", "video"] | None = None
    """What an omni worker emits, rendered as `--output-modalities`. None on every chat deployment.

    None rather than `text`, so an operator that predates the field and one that knows it read a
    chat deployment's frame the same way.
    """

    diffusion: DiffusionSpec | None = None
    """The omni worker's stage configuration, or None to leave omni's defaults in place."""


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
    """What one deployment asks a cluster for.

    `name` is on the wire because the operator names what it renders after it, and
    every Kubernetes object below that one inherits the name. Sending only the id
    put `dep-01m0sktt1s3vszhp5rkdp925dh-...` on every pod, which is unreadable to
    the person running `kubectl get pods` and to the console showing them.

    `min_replicas` and `max_replicas` are THIS CLUSTER'S SHARE, never a workload
    total. A workload declares totals across every cluster it may use and the
    plane divides them before they reach the wire; an operator sees one cluster
    and cannot tell a whole workload from a fragment of one, which is the point.
    A receiver renders exactly the number it is sent and must never sum, scale or
    re-derive it against anything else. `min_replicas` is what is rendered;
    `max_replicas` is a ceiling for an autoscaler that does not exist yet.

    There is deliberately no workload id here and no total-versus-share field of
    any kind. Nothing in a cluster ever needs to know it is one of several.

    `served_model_name` is the model id an API client sends, already resolved: the
    workload's own declaration, or the artifact's lowercase Hugging Face slug when
    it declares none. The plane resolves it so the operator holds no derivation
    logic and renders the string it is told. It is never empty, and
    `engine_args` still wins over it at render time — the escape hatch is laid
    over the derived flags, which is why the resolved hatch is deliberately not
    folded in here.

    `serving` carries the engine configuration this deployment renders. It is
    inlined rather than referenced; see `ServingSpec` for why.
    """

    deployment_id: str
    name: str
    cluster_id: str
    project_id: str
    lifecycle: Literal["service", "task", "gang"]
    priority_class: str
    min_replicas: int
    max_replicas: int
    placement: PlacementConstraints
    serving: ServingSpec
    artifact_ids: list[str]
    served_model_name: str
    generation: int
    updated_at: str
    authority: Literal["observed", "managed"] = "observed"
    adopt: Adopt | None = None
    role_replicas: dict[str, int] = {}
    """The absolute worker count per role, already multiplied by the plane. Empty when aggregated.

    Absolute, and the multiplication is the point. Under a disaggregated topology `min_replicas`
    counts **groups**, and a group is the per-role weights taken together — so a role's count is
    `min_replicas x weight`. The plane does that arithmetic because the operator is forbidden it:
    a receiver "renders exactly the number it is sent and must never sum, scale or re-derive it
    against anything else", as this class says of `min_replicas` above. Making the operator
    multiply would be the first exception to an invariant that is currently absolute and cheap to
    check, and the alternative is one extra map on a frame the plane already sends.
    """


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


class RoleObserved(Struct):
    """One role's own share of a deployment's rollout, as its cluster counts it.

    `desired` is the `role_replicas` count the cluster rendered, echoed back, and
    never a number the operator derived — the same rule `desired_replicas` is held
    to one level up.
    """

    role: Literal["prefill", "decode"]
    desired: int
    ready: int


class DeploymentObserved(Struct):
    """What one cluster reports about the deployment it was handed.

    `desired_replicas` is what this cluster rendered — its own `min_replicas`,
    echoed back so the plane can see that render and declaration agree. Summing
    it across a workload's deployments is the plane's job and only the plane's;
    the operator never does.

    `replica_ids` is every replica this cluster has for this deployment, and it
    is the only thing that lets the plane forget one. Demand state is not pruned
    against a resync, so a replica that stopped being reported used to stay in
    the database for ever and keep counting — a deployment of one reading three
    ready of one. `None` is an operator that predates the field and makes no
    claim; an empty list is a claim that there are none, which is what a scale
    to zero says.
    """

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
    reason: str | None = None
    message: str | None = None
    replica_ids: list[str] | None = None
    roles: list[RoleObserved] | None = None
    """The same rollout split by role, beside the four scalars rather than instead of them.

    The scalars stay the group rollup, so a plane reading only those is right and
    merely coarse. The precision is what the split is for: a 1P:2D group with two
    ready decode workers and no prefill is `2 of 3`, which reads as a shortfall
    that will resolve itself, where in fact nothing is serving — a decode worker
    with no prefill peer has nothing to decode from — and no arrangement of the
    totals can say so.

    `None` from an aggregated deployment and from an operator that predates the
    field, and those are the same claim: this deployment has no roles worth
    naming. An empty list is never sent.
    """


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
    serving_digest: str
    restart_count: int
    observed_generation: int
    observed_at: str
    started_at: str | None = None
    engine_runtime_version: str = ""
    """A tag of the Dynamo runtime this replica started under, echoed from the deployment.

    Defaulted rather than required so that an operator older than the rename, which sends
    the value under `engine_version`, is not rejected outright — one missing required field
    fails the whole frame, and the replica rollup for a plainly serving deployment would sit
    at zero. Read through `runtime_version`, never directly.
    """
    engine_version: str = ""
    """The same value under the name an operator older than the rename sends it as."""
    role: Literal["prefill", "decode"] | None = None
    """Which role of a disaggregated deployment this replica is, and `None` when it is not one.

    Read by the operator off the pod label it stamped itself rather than off the pod's
    name, so it is a string both sides agreed on rather than Dynamo's naming convention
    seen from the outside. Observed, with ingestion as its only writer — which is what
    keeps a worker that can change role at runtime a value that moves rather than a
    declaration that has to be revised.
    """

    @property
    def runtime_version(self) -> str:
        """The new spelling when the operator sent one, else the old."""
        return self.engine_runtime_version or self.engine_version


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
    reason: str = ""
    message: str = ""
    """Why this state and not another, in the cluster's own words.

    Free text with an empty default, so an operator that predates these fields
    still decodes. `absent` was previously indistinguishable between "the
    download has not started yet" and "nothing on this cluster can start it",
    and those need different actions from whoever is reading the page.
    """


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
    kind: Literal["cluster", "node", "device", "deployment", "workload", "component", "artifact"]
    id: str


class OperationDesired(Struct):
    operation_id: str
    cluster_id: str
    kind: Literal["drain", "cordon", "reboot", "device_reset", "cache_warm", "roll", "upgrade", "benchmark"]
    target: OperationTarget
    reason: str
    requested_by: str
    deadline: str
    parameters: dict[str, str]
    created_at: str


class OperationCancel(Struct):
    """Withdraws an operation the plane has already sent.

    A message of its own rather than a field on `OperationDesired`. The operation
    is already accepted and probably already running, so what travels here is a
    second imperative about the first, not a restatement of it — and re-sending
    the desired frame with a flag on it would make the withdrawal prunable by a
    resync, which is the property operations are kept outside the snapshot to
    avoid.

    There is no reply of its own. The operator answers on `operation.observed`
    with a terminal phase, the same as it would for an operation that ended by
    itself, and ingestion closes the row from there.
    """

    operation_id: str
    cluster_id: str
    reason: str
    requested_by: str
    requested_at: str


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


# --------------------------------------------------------------------------- benchmarks


class BenchmarkObserved(Struct):
    """The outcome of a benchmark run, in whatever shape the harness emits.

    Beside `operation.observed` rather than folded into it, because
    `OperationObserved.result` is `dict[str, str]` and every load generator worth
    running emits nested JSON. Widening that field would widen it for drain and
    cordon too, which have nothing to put in it.

    `summary` is not typed past `dict`. What AIPerf calls a percentile and what
    vLLM's benchmark script calls one are not the same field, and a schema here
    asserting they are would be this protocol inventing a normalisation nobody
    has yet had cause to design. It is stored verbatim and `harness_version` is
    what makes two of them comparable.

    `artifacts_uri` is where the per-request records went. They are telemetry —
    high volume, append-only, never joined — so they go from the cluster to
    object storage directly and only the reference travels this stream.
    """

    benchmark_id: str
    cluster_id: str
    deployment_id: str
    harness: str
    phase: Literal["pending", "running", "succeeded", "failed", "cancelled", "timed_out"]
    observed_at: str
    harness_version: str | None = None
    summary: dict | None = None
    artifacts_uri: str | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    # The harness's stdout, tail-capped by the operator. Object storage is the right home
    # for the full log; this field is what a reader sees when there was no `artifacts_uri`
    # to follow or the run never got that far.
    log_tail: str | None = None


MESSAGE_TYPES: dict[str, type[Struct]] = {
    "hello": Hello,
    "welcome": Welcome,
    "ping": Ping,
    "pong": Pong,
    "error": Error,
    "ack": Ack,
    "stream.end": StreamEnd,
    "resync.begin": ResyncBegin,
    "cluster.observed": ClusterObserved,
    "node.observed": NodeObserved,
    "node.event": NodeEvent,
    "device.observed": DeviceObserved,
    "device.event": DeviceEvent,
    "topology.observed": TopologyObserved,
    "capacity.observed": CapacityObserved,
    "workload.discovered": WorkloadDiscovered,
    "deployment.desired": DeploymentDesired,
    "deployment.deleted": DeploymentDeleted,
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
    "operation.cancel": OperationCancel,
    "benchmark.observed": BenchmarkObserved,
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
