import datetime as dt

from msgspec import field

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType
from app.core.serving import AGGREGATED
from app.modules.deployments.schemas import DeploymentStatusSchema
from app.modules.deployments.schemas import DeploymentReplicaRollupSchema


class ShapeSchema(Struct):
    """The hardware a workload implies, class first.

    `device_class` is not optional and is not separable from the counts: a
    tensor-parallel-8 configuration means nothing spread across an A100 and an
    H100, so a count without a class is not a shape.

    The topology pair sits here rather than beside the scale numbers because it is
    a property of one replica's layout, the same way the counts are. It used to be
    declared twice — once on the recipe and once as a nullable override on the
    workload — and with one object there is one answer.
    """

    device_class: str
    device_count: int = 1
    device_memory_mib_min: int = 0
    requires_single_topology_domain: bool = False
    topology_domain_kind: str | None = None


class ParallelismSchema(Struct):
    tensor: int = 1
    pipeline: int = 1
    expert: int = 1


class CacheSchema(Struct):
    """KV and prefix cache configuration. Unrelated to the weight cache.

    The weight cache is a declared warm-set per cluster with observed residency;
    this is per-request attention state and is configuration on the workload. They
    are two different things and do not share an object.
    """

    kv_cache_dtype: str | None = None
    kv_cache_fraction: float | None = None
    prefix_cache_enabled: bool = True
    prefix_cache_config: dict = {}


class ReasoningSchema(Struct):
    """How this workload thinks, as rendered flags. What it *can* do is in `capabilities`.

    Its own struct for the reason `CacheSchema` is: the fields are meaningless
    individually. All of them are conditioned on the model being able to think at
    all, and flattened onto the serving block they would read as six unrelated
    optionals with nothing to group them under.
    """

    default_on: bool = False
    parser: str | None = None
    transport: str = "reasoning_content"
    think_begin_token: str | None = None
    think_end_token: str | None = None
    suppress_eos_until_think_end: bool = False


class DiffusionSchema(Struct):
    """What a vLLM-Omni worker reads from its stage-config file rather than its command line.

    Two fields because only two vary per model. The operator writes the rest of the file —
    one stage, the devices the pod holds, the executor — and the Dynamo omni CLI refuses
    `--model-class-name`, so the pipeline class has no other way in. `default_sampling_params`
    exists because omni defaults every model to 50 inference steps, which is wrong for every
    distilled model: FLUX.2-klein wants 4 and Z-Image-Turbo 8. A request's own values still win.
    """

    model_class_name: str | None = None
    default_sampling_params: dict = {}


class ServingSchema(Struct):
    """What the served API does, as the engine will actually be told to behave.

    Every field renders to a flag or a rendered value. Honouring any of it means
    a replica restart, which is why it lives beside `cache` on the workload rather
    than on the endpoint. Before it existed all of this went into `engine_args`,
    where the plane could neither validate it nor say what it had asked for.

    Every default is the behaviour of a workload that says nothing, so the block is
    inert on an existing workload: an empty parser, an empty tool parser and
    `suppress_eos_until_think_end` false all mean "emit no flag".
    """

    reasoning: ReasoningSchema = field(default_factory=ReasoningSchema)
    stream_usage: str = "final_chunk"
    context_overflow: str = "reject"
    structured_output_backend: str | None = None
    tool_call_parser: str | None = None
    max_images_per_request: int | None = None
    max_videos_per_request: int | None = None
    output_modality: str | None = None
    """What the engine emits: `image` or `video` on the omni engine, null (text) on every other.

    Here and not on `capabilities`, because it renders — `--output-modalities` on the worker —
    and because the routing table carries it to the gateway, which refuses a path the upstream
    does not answer.
    """
    diffusion: DiffusionSchema | None = None
    """The omni worker's stage configuration, null to leave omni's own defaults in place."""


class CapabilitiesSchema(Struct):
    """What this engine × artifact is claimed to be able to do. Asserted, never rendered.

    The same split as `engine_args` against `imported_args`, one level up: nothing
    here becomes a flag or reaches a cluster, because a cluster has no use for a
    claim about itself. The plane checks these against the workload's own
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


class ResourcesSchema(Struct):
    cpu_cores: int | None = None
    memory_mib: int | None = None
    disk_gib: int | None = None


class OverlaySchema(Struct):
    """Passthrough for what the plane deliberately does not model.

    `pod_spec` merges over the rendered pod with strategic-merge semantics;
    `dynamo` merges over the rendered graph deployment, where maps merge and
    lists replace.
    """

    pod_spec: dict = {}
    dynamo: dict = {}


class RouterSchema(Struct):
    """How this workload's own Dynamo frontend routes requests between its workers.

    One field per Dynamo setting, each rendered as one `DYN_ROUTER_*` variable on the frontend and
    nothing else. Null renders nothing and leaves Dynamo's default in place, except `mode`, where
    null is the operator's default of `kv`, or `round-robin` for an omni workload.

    - `mode` — `kv`, `round-robin`, `random`, `power-of-two`, `least-loaded` or
      `device-aware-weighted`.
    - `queue_threshold` — a fraction of a worker's batched-token budget above which the router holds
      requests back rather than dispatching them. Null means the router does not queue.
    - `queue_policy` — `fcfs` or `wspt`, the order a queued request leaves in. Needs a threshold.
    - `kv_overlap_score_credit` — how much a cached prefix discounts a worker's prefill load when the
      KV router picks one.

    Typed rather than an environment passthrough, so the plane can refuse a value Dynamo would refuse
    at startup and say what it asked for. It replaces whole on a patch, like `cache`.
    """

    mode: str | None = None
    queue_threshold: float | None = None
    queue_policy: str | None = None
    kv_overlap_score_credit: float | None = None


class RoleSchema(Struct):
    """One role of a disaggregated workload: its share of the group, and what makes it different.

    Three fields because the difference between prefill and decode is three fields wide. Across
    NVIDIA's published recipe the two roles agree on the artifact, the tensor degree, the device
    class, the context length, the quantization and the `kv-cache-dtype` — and on
    `kv-transfer-config`, which is literally the same string on both sides, because the transfer
    mechanism belongs to the topology rather than to either role. A schema giving each role a full
    copy of the serving block would be charging for a difference that does not exist.

    `weight` is scale and the other two are configuration, and that split is load-bearing rather
    than tidy: the weights are deliberately outside `serving_digest` and the other two are inside
    it. See `app.core.serving.serving_digest`.
    """

    role: str
    weight: int = 1
    max_batch_tokens: int | None = None
    engine_args: dict = {}


class TopologySchema(Struct):
    """How a workload's prefill and decode run: one process, or two that exchange a KV cache.

    A block rather than two loose fields so that the aggregated case is expressible by its absence.
    `roles` on an aggregated workload is refused rather than ignored — silently dropping a declared
    ratio would let somebody believe they had asked for something.

    Read back with `group_devices` filled in, which is derived and never stored. It is the whole
    reason this is a block on the response as well as on the request: a person declaring 1:2 at TP8
    has asked for 24 devices per unit, and placement will refuse or strand in that unit rather than
    in the 8 they typed. Telling them the number at declaration is cheaper than a workload that
    expands to nothing.
    """

    serving_topology: str = AGGREGATED
    roles: list[RoleSchema] = []


class TopologyViewSchema(Struct):
    """`TopologySchema` as a read returns it, with the derived numbers beside it."""

    serving_topology: str = AGGREGATED
    roles: list[RoleSchema] = []
    group_devices: int = 0
    """Devices one indivisible unit costs: `sum(weight x device_count)` over the roles.

    Derived on every read and stored nowhere. A column holding it would put the role weights into
    `serving_digest` through a second door — `device_count` is a digest field — and it would do it
    silently, because nothing about such a column's name would say whether it held the group's
    total or one worker's.
    """


class WorkloadServingSchema(Struct):
    """How the workload serves its weights, as one block.

    Grouped rather than flattened onto the workload because the two halves answer
    different questions: everything here decides what process runs, and everything
    outside it decides how much of it runs and where. `serving_digest` is a hash of
    exactly this block, which is only a coherent statement while the block is a
    thing you can point at.

    `serving` inside `serving` is not a slip. The outer block is the whole serving
    configuration; the inner one is the typed API-behaviour field group that lands
    in the `serving` column, and giving it a third name here would leave the HTTP
    body, the column and the digest field list each calling it something different.

    `resolved_engine_args` is derived on every read and stored nowhere. It is the
    declared `engine_args` with the flags the typed `serving` block dictates laid
    over the top, keyed the way the operator keys them — without the leading
    dashes. It exists so that "which value actually took effect" is a thing the
    console can show rather than a thing a reader has to reconstruct from two
    fields with overlapping meaning.
    """

    artifact_id: str
    engine: str
    engine_runtime_version: str
    engine_runtime_image: str | None
    frontend_image: str | None
    engine_args: dict
    imported_args: dict
    quantization: str | None
    dtype: str | None
    parallelism: ParallelismSchema
    context_length: int
    max_batch_tokens: int | None
    cache: CacheSchema
    serving: ServingSchema
    capabilities: CapabilitiesSchema
    resolved_engine_args: dict
    shape: ShapeSchema
    resources: ResourcesSchema
    overlay: OverlaySchema
    topology: TopologyViewSchema
    router: RouterSchema = field(default_factory=RouterSchema)


class WorkloadServingRequest(Struct):
    """The serving configuration a workload is created with.

    Shaped like the block a read returns, so the object a caller sends and the
    object it gets back are the same object. There is no id here and no name: a
    serving configuration is identified by its content — `serving_digest` — and
    two workloads that ask for the same engine, weights and shape carry the same
    digest without either of them having to find the other first.

    `imported_args` is accepted so that a workload promoted from something already
    running keeps the argv it was read with. It is never rendered and never
    hashed; declaring a flag is `engine_args` and reading one is this.
    """

    artifact: str
    engine: str
    engine_runtime_version: str
    shape: ShapeSchema
    context_length: int = 0
    """The engine's `--max-model-len`. Required of every text engine; a diffusion model has none.

    Defaulted rather than required so an omni workload can leave it out: 0 is stored, hashed,
    sent, and rendered by nothing. On any other engine `check_shape` refuses anything below 1,
    which is what omitting it always amounted to.
    """
    engine_runtime_image: str | None = None
    frontend_image: str | None = None
    engine_args: dict = {}
    imported_args: dict = {}
    quantization: str | None = None
    dtype: str | None = None
    parallelism: ParallelismSchema = field(default_factory=ParallelismSchema)
    max_batch_tokens: int | None = None
    cache: CacheSchema = field(default_factory=CacheSchema)
    resources: ResourcesSchema = field(default_factory=ResourcesSchema)
    overlay: OverlaySchema = field(default_factory=OverlaySchema)
    serving: ServingSchema = field(default_factory=ServingSchema)
    capabilities: CapabilitiesSchema = field(default_factory=CapabilitiesSchema)
    topology: TopologySchema = field(default_factory=TopologySchema)
    router: RouterSchema = field(default_factory=RouterSchema)


class WorkloadServingUpdate(Struct):
    """A partial update of the serving configuration. An absent field is untouched.

    `artifact` is absent by design. A workload is a reproducible way to serve some
    specific weights, and repointing it at other weights makes every deployment
    expanded from it mean something it did not mean yesterday — that is a new
    workload, and it is cheap.

    The sub-blocks replace whole. `parallelism`, `cache`, `shape`, `resources`,
    `serving`, `capabilities` and `router` are groups whose fields are meaningless apart, so
    a patch that named half of one would leave the rest at its defaults rather
    than at what was stored, and the caller could not tell which it got. Sending a
    block means stating it.
    """

    engine: str | UnsetType = UNSET
    engine_runtime_version: str | UnsetType = UNSET
    engine_runtime_image: str | None | UnsetType = UNSET
    frontend_image: str | None | UnsetType = UNSET
    engine_args: dict | UnsetType = UNSET
    imported_args: dict | UnsetType = UNSET
    quantization: str | None | UnsetType = UNSET
    dtype: str | None | UnsetType = UNSET
    parallelism: ParallelismSchema | UnsetType = UNSET
    context_length: int | UnsetType = UNSET
    max_batch_tokens: int | None | UnsetType = UNSET
    cache: CacheSchema | UnsetType = UNSET
    shape: ShapeSchema | UnsetType = UNSET
    resources: ResourcesSchema | UnsetType = UNSET
    overlay: OverlaySchema | UnsetType = UNSET
    serving: ServingSchema | UnsetType = UNSET
    capabilities: CapabilitiesSchema | UnsetType = UNSET
    topology: TopologySchema | UnsetType = UNSET
    router: RouterSchema | UnsetType = UNSET


class WorkloadDeploymentSchema(Struct):
    """One cluster's share of a workload, as seen from the workload.

    Enough to render the per-cluster table and link to the deployment; the full
    per-cluster object is `GET /v1/deployments/{ref}`.
    """

    id: str
    name: str
    cluster_id: str
    cluster_name: str
    region: str | None
    min_replicas: int
    max_replicas: int
    desired_replicas: int
    admission_phase: str
    admission_reason: str | None
    generation: int
    enabled: bool
    authority: str
    queued_at: dt.datetime | None
    admitted_at: dt.datetime | None
    placed_at: dt.datetime | None
    replicas: DeploymentReplicaRollupSchema
    status: DeploymentStatusSchema | None
    live_digests: dict[str, int] = {}
    """Ready replicas in this cluster per serving configuration they actually came up on.

    The observed answer to "which version is live", and a map rather than a string because
    during a roll there are two and the pair is the whole point. One entry is a settled
    deployment; two is a rollout in progress; an entry that is not the workload's current
    `serving_digest` is a cluster still running what it was asked for last time.

    Empty when no ready replica has reported a digest — a cluster whose agent has not resynced
    since the column existed, or an operator too old to stamp the annotation. Empty means not
    known, never "none".
    """


class WorkloadRevisionUpdate(Struct):
    """The only write a revision accepts, and it is a comment.

    A revision is otherwise append-only: `serving`, `serving_digest` and `artifact` are a record
    of what ran and editing one would be editing history. The note is the exception because it
    is not part of the record — it is somebody's account of it, and an account is frequently
    written after the fact, when the benchmark that explains the change has finished.
    """

    note: str | None


class WorkloadRevisionSchema(Struct):
    """One entry in a workload's serving history, and the body that would restate it.

    `serving` is a `WorkloadServingUpdate` as it was stored, which makes it exactly the body
    that restates this configuration: `PATCH /v1/workloads/{ref}` with `{"serving": <this>}`,
    validated on the way in by the same checks that validate a hand-written patch. That is the
    whole of rollback, and why there is no operation for it.

    It is handed back as an object rather than re-typed through the patch struct on purpose. A
    snapshot is a record of what was asked for at the time, and decoding it through today's
    struct would re-default a field added since and reject one removed since — making the oldest
    revisions, the ones most worth having, the first to become unreadable.

    `artifact` sits beside it rather than inside it because the weights are not patchable: a
    workload is a reproducible way to serve some specific weights, and repointing it is a new
    workload. It is here so that a revision is a complete record of what ran, and a reader can
    see that a configuration from before a weights swap is not one to reapply blind.

    `number` counts from 1 within the workload. It never rewinds, so going back to revision 2
    appends a new one carrying revision 2's snapshot rather than deleting what came after.
    """

    id: str
    number: int
    serving_digest: str
    artifact: str
    reason: str
    note: str | None
    created_at: dt.datetime
    serving: dict


class WorkloadScaleSchema(Struct):
    """Declared totals, and what the shares actually add up to.

    `unit` is named by kind — `replicas` for service, `ranks` for gang, `completions`
    for task — because the three numbers count different things and a bare integer in
    a UI is unreadable without it. `placed` is the sum of the deployments' shares and
    `ready` the sum of their observed ready counts; they are derived here and are the
    only place summing is legal.
    """

    unit: str
    min: int
    max: int
    desired: int
    placed: int
    ready: int


class WorkloadSchema(Struct):
    """A workload: what was asked for, and where it ended up.

    `deployments` is the per-cluster expansion, one row per cluster placement chose.
    A `gang` workload always has exactly one; splitting a gang across clusters is
    rejected at placement, not ranked down.

    `phase` is derived from the deployments and is not stored: `running` when every
    deployment is running, `failed` if any failed, otherwise the least advanced
    phase among them. Nothing writes it.

    `serving_digest` is derived from `serving` on every write and is never accepted
    from a client. It is what a benchmark run and a wire frame both name, so that
    "the same configuration" is one string rather than a comparison of eight fields.
    """

    id: str
    name: str
    kind: str
    phase: str
    project_id: str
    priority_class: str
    preemptible: bool
    paused: bool
    gang_size: int
    serving_digest: str
    serving: WorkloadServingSchema
    adapter_artifact_ids: list[str]
    served_model_name: str
    """The model id the engine registers under. Read from the artifact's slug and never declared.

    Reported rather than accepted, which is the change: the workload used to carry a nullable
    override of it and the plane resolved between the two. Two writeable spellings of one public
    identifier is how the id a client sends and the id a frontend registered come apart, and that
    failure is a 404 from inside the cluster with the deployment healthy. What a *client* sends is
    still changeable — that is `Endpoint.slug`, and the gateway rewrites between them.
    """
    overrides: dict
    labels: dict
    strategy: str
    cluster_floors: dict
    max_clusters: int | None
    pinned_cluster_id: str | None
    scale: WorkloadScaleSchema
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    deadline_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    deployments: list[WorkloadDeploymentSchema]
    artifact_tag: str | None


class WorkloadPlacementNodeSchema(Struct):
    """One node a workload's live devices currently sit on, with its occupancy.

    `device_count` is how many of the node's devices this workload holds right
    now — the node's slot occupancy from the workload's point of view — derived
    from the live allocation rows rather than declared anywhere.
    """

    node: str
    cluster: str
    device_count: int


class WorkloadReplicaPlacementSchema(Struct):
    """Where one live allocation of a workload landed, replica by replica.

    `replica` is null for an allocation the cluster has not yet tied to a replica
    — a held allocation is a commitment of devices that may precede the process
    that runs on them. `gang_complete` says whether the allocation currently holds
    every device it was granted, so a partially satisfied gang is visible here.
    """

    allocation: str
    replica: str | None
    cluster: str
    nodes: list[str]
    devices: list[str]
    device_count: int
    gang_complete: bool


class WorkloadPlacementSchema(Struct):
    """Where a workload physically is, derived from its live allocations.

    Read-only and computed at read time from observed allocation, device and node
    rows scoped to the workload. Only membership and placement facts appear —
    no serving telemetry. `node_count` and `gpu_count` are the distinct live nodes
    and devices the workload holds across every cluster it landed on.
    """

    workload: str
    node_count: int
    gpu_count: int
    nodes: list[WorkloadPlacementNodeSchema]
    replicas: list[WorkloadReplicaPlacementSchema]


class WorkloadCreateRequest(Struct):
    """What it takes to run something, without saying where.

    `serving` is how the weights are served, and it is stated here rather than
    named: there is no shared serving object to point at any more, because the
    sharing never paid for the ordering rule and the resume race it cost. Two
    workloads asking for the same thing land on the same `serving_digest`, which is
    what the console used to reconstruct by comparing eight fields.

    `cluster` is optional, and an absent one is the normal case: placement picks,
    from the serving block's device shape, the project's quota, the policies attached
    to the workload, and what the clusters currently report free. Naming one narrows
    the candidates to that cluster and forces a single deployment there; it is still
    checked for phase, reachability, policy, quota and capacity, and a pin at a cluster
    that cannot take the workload leaves it pending with that cluster's reasons.

    `kind` is `service`. `task` and `gang` are reserved for workload kinds on the
    roadmap and are refused at creation, because no operator renders them yet.

    `min_scale` / `max_scale` / `desired_scale` are TOTALS across every cluster this
    workload may use, counted in replicas.

    `priority_class` is optional rather than absent: every workload carries one, but
    the project's default answers for most callers and making them repeat it is how
    defaults drift.

    `allowed_regions` and `allowed_clusters` narrow placement without taking the
    decision off it, which is what separates them from `cluster`: placement still runs,
    still reads quota, still checks topology and capacity, and still picks — it just has
    fewer clusters to pick from. "Any cluster in `ap`" is the common form and the reason
    the region is a first-class field rather than a cluster list somebody has to keep up
    to date as the fleet grows.

    They are here rather than left to `POST /policies` because placement expands inside
    `create`: a policy attached after the response has already missed the decision it
    was meant to constrain. What is created *is* a Policy — named after the workload and
    attached to it — so the constraint is discoverable, editable and enforced on every
    later expansion by the same machinery as any other policy. Both lists empty means no
    opinion, never "nothing allowed".
    """

    serving: WorkloadServingRequest
    name: str
    project: str
    cluster: str | None = None
    allowed_regions: list[str] = []
    allowed_clusters: list[str] = []
    priority_class: str | None = None
    kind: str = "service"
    min_scale: int = 1
    max_scale: int = 1
    desired_scale: int = 1
    strategy: str = "pack"
    cluster_floors: dict = {}
    max_clusters: int | None = None
    enabled: bool = True
    preemptible: bool = False
    gang_size: int = 1
    deadline_at: dt.datetime | None = None
    adapter_artifacts: list[str] = []
    overrides: dict = {}
    labels: dict = {}
    note: str | None = None
    """Why this configuration, in your own words. Lands on the revision this create files.

    Not a description of the workload — that is what `labels` and the name are for. It
    describes the *configuration*, and it travels with the revision rather than the row, so it
    is still attached to this setting after three edits have moved on from it.
    """


class WorkloadUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `serving` is patchable where the recipe reference it replaces was not, because
    the reason it was not is gone: repointing a deployment at a different recipe was
    a change of object identity, and changing the engine version of a workload is a
    change to the workload. The digest is recomputed from what the write left behind
    and never taken from the caller.

    `cluster` is absent by design — a deployment is workload × cluster, so moving one
    is a re-expansion and `workload.drain` owns it. Scale is absent too: changing it is
    long-running and can sit pending on quota forever, so it is the `workload.scale`
    operation and not a field write (rule 1).
    """

    name: str | UnsetType = UNSET
    serving: WorkloadServingUpdate | UnsetType = UNSET
    adapter_artifacts: list[str] | UnsetType = UNSET
    overrides: dict | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
    priority_class: str | UnsetType = UNSET
    preemptible: bool | UnsetType = UNSET
    deadline_at: dt.datetime | None | UnsetType = UNSET
    strategy: str | UnsetType = UNSET
    cluster_floors: dict | UnsetType = UNSET
    max_clusters: int | None | UnsetType = UNSET
    note: str | None | UnsetType = UNSET
    """Why this change, written at the moment somebody still knows.

    It lands on the revision this patch files. A patch that files none — one carrying only a
    note, or one that arrives at the configuration already in force — puts it on the revision
    currently in force instead, so a note is never silently dropped and commenting on what is
    running needs no separate call.

    It is the one field here that reaches a revision rather than the workload, and the only
    mutable thing about a revision: what the configuration was is a fact, why it changed is
    often understood later.
    """
