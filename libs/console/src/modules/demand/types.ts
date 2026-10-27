/** `ArtifactLocationSchema`. No id: `uri` is unique within the artifact. */
export interface ArtifactLocation {
  scheme: string
  uri: string
  region: string | null
  preferred: boolean
}

/**
 * `ArtifactUpstreamSchema`: what the publisher claims for this release, with provenance. Never
 * reaches a cluster. Its `licence` is the model card's; a mismatch with `Artifact.licence` is a
 * finding.
 */
export interface ArtifactUpstream {
  provider: string | null
  provider_model_id: string | null
  context_length: number | null
  max_output_tokens: number | null
  input_modalities: string[]
  /** Null is "the publisher did not say", which is not the same as `false`. */
  reasoning: boolean | null
  tool_calling: boolean | null
  structured_output_modes: string[]
  licence: string | null
  released_at: string | null
  source_uri: string | null
  recorded_by: string | null
  recorded_at: string | null
}

/** The write shape of `upstream`. Replaced wholesale, the way locations are. */
export type ArtifactUpstreamRequest = Partial<ArtifactUpstream>

/**
 * `ArtifactBaselineSchema`: a declared, signed quality floor. `tolerance_pct` is the recorder's
 * slack, not the acceptance floor.
 */
export interface ArtifactBaseline {
  id: string
  artifact_id: string
  dataset: string
  metric: string
  value: number
  tolerance_pct: number
  source_uri: string | null
  recorded_by: string
  recorded_at: string
  note: string | null
  created_at: string
  updated_at: string | null
  revision: string
}

/** `ArtifactSchema` — immutable, content-addressed bytes. Identity is the digest. */
export interface Artifact {
  id: string
  digest: string
  verified: boolean
  name: string
  kind: string
  format: string | null
  size_bytes: number
  licence: string | null
  parameter_count: number | null
  base_artifact_id: string | null
  project_id: string | null
  /** Absent on sparse payloads: the plane may return an artifact with no upstream. */
  upstream?: ArtifactUpstream
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string | null
  revision: string
  locations: ArtifactLocation[]
}

/** `ArtifactUsageSchema`: dependent workloads and derived artifacts, counted at read time. */
export interface ArtifactUsage {
  artifact_id: string
  workload_count: number
  downstream_count: number
}

/** `fleet`: classes a cluster reported. `catalog`: the contract table's too (may need hardware). */
export const FITMENT_SCOPES = ["fleet", "catalog"] as const

export type FitmentScope = (typeof FITMENT_SCOPES)[number]

/** `ArtifactFitLayoutSchema`: another serving device count for the class, ascending. */
export interface ArtifactFitLayout {
  device_count: number
  node_count: number
  tensor_parallel: number
  pipeline_parallel: number
  aggregate_gb: number
  required_gb: number
  context_length: number
  /** This count buys its cards back with a shorter window than the one asked for. */
  reduced: boolean
  /** The fleet holds fewer devices of the class than this layout needs. */
  supply_short: boolean
  reason: string
}

/**
 * `ArtifactFitSchema`: whether this serves on one class. `fits` is physics; the counts are
 * capacity.
 */
export interface ArtifactFit {
  device_class: string
  label: string
  fits: boolean
  /** Weights fit and heads divide. False is a refusal; `fits` false with this true is advice. */
  possible: boolean
  /** The window had to be cut below the one asked for. Deliberately not a failure. */
  reduced: boolean
  device_count: number
  /** Nodes the replica spans: the unit that gets ordered. */
  node_count: number
  /** Observed node size for a known class, assumed otherwise (8 on HGX, 72 on NVL72). */
  devices_per_node: number
  /** Inside a node, where the interconnect is coherent. Capped by `devices_per_node`. */
  tensor_parallel: number
  /** Across nodes, over the backend fabric. Those nodes are one connected unit. */
  pipeline_parallel: number
  memory_mib_per_device: number
  aggregate_gb: number
  weights_gb: number
  required_gb: number
  context_length: number
  /** False without an attention shape: `required_gb` is weights only, a floor. */
  kv_counted: boolean
  reason: string
  devices_total: number
  devices_allocatable: number
  cluster_count: number
  /** The fleet has this class but fewer than one replica needs. */
  supply_short: boolean
  /** False for hardware the contract table has never named, fitted from what a cluster reported. */
  in_catalog: boolean
  /** Every serving count on this class, the recommendation included. */
  layouts: ArtifactFitLayout[]
}

/** `ArtifactFitmentSchema`: classes ranked, inputs echoed. `sizable` false: no parameter count. */
export interface ArtifactFitment {
  artifact_id: string
  scope: FitmentScope
  sizable: boolean
  quantization: string
  context_length: number
  concurrency: number
  max_devices: number
  /** How many classes were ranked. `items` is the head of that list, not all of it. */
  considered: number
  note: string | null
  items: ArtifactFit[]
}

/** A fitment row seeded into a workload's serving block. */
export interface ServingFootprint {
  device_class: string
  device_count: number
  tensor_parallel: number
  pipeline_parallel: number
  context_length: number
  quantization: string
}

/** Query parameters for a fitment. Every one is optional and defaulted by the plane. */
export interface FitmentParams {
  scope?: FitmentScope
  context_length?: number
  concurrency?: number
  quantization?: string
  /** Only applies to classes the fleet has never reported; observed figures win. */
  devices_per_node?: number
  max_devices?: number
  /** Ranked rows to return. Zero or below means every one of them. */
  limit?: number
}

/** `ArtifactLocationRequest` — the write shape of a location. */
export interface ArtifactLocationRequest {
  scheme: string
  uri: string
  region?: string | null
  preferred?: boolean
}

export interface ArtifactCreateRequest {
  /** Omitted when `source` is set: the plane derives identity, the client never asserts it. */
  digest?: string
  /** `owner/name`, `hf:owner/name` or `hf://owner/name`. Mutually exclusive with `digest`. */
  source?: string
  name: string
  verified?: boolean
  kind?: string
  format?: string | null
  size_bytes?: number
  licence?: string | null
  parameter_count?: number | null
  base_artifact?: string | null
  project?: string | null
  upstream?: ArtifactUpstreamRequest
  metadata?: Record<string, unknown>
  locations?: ArtifactLocationRequest[]
}

/** The digest is identity, so it is absent here: bytes are never re-pointed. */
export interface ArtifactUpdateRequest {
  name?: string
  kind?: string
  format?: string | null
  size_bytes?: number
  licence?: string | null
  parameter_count?: number | null
  project?: string | null
  upstream?: ArtifactUpstreamRequest
  metadata?: Record<string, unknown>
  locations?: ArtifactLocationRequest[]
}

/** `ServingDefaults`: implied by the weights. Seeds the form; the plane never applies them. */
export interface ServingDefaults {
  device_count: number
  device_memory_mib_min: number
  context_length: number | null
  quantization: string | null
  dtype: string | null
}

/** `UpstreamDeployment`: the corpus's layout for one node shape, not a fleet fact. */
export interface UpstreamDeployment {
  hardware: string
  display_name: string
  gpu_count: number
  tensor_parallel: number
  nodes_declared: number
  nodes_by_memory: number
  strategies: string[]
}

/**
 * `UpstreamRecipe`: a third party's tested configuration, attributed, kept apart from
 * `ServingDefaults` so each seeded value says where it came from.
 */
export interface UpstreamServing {
  source: string
  /** The recipe on the corpus's own branch — what upstream says about this now. */
  source_uri: string
  /** The same file at the commit these values were captured from. */
  snapshot_uri: string
  model_id: string
  variant: string
  precision: string | null
  engine: string
  /** Lowest vLLM release. Shown, never seeded: it is not a Dynamo runtime tag. */
  engine_version_min: string | null
  nightly_required: boolean
  /** The publisher's own image. Reported, never seeded: it has no Dynamo in it. */
  docker_image: string | null
  tensor_parallel: number | null
  default_strategy: string | null
  deployments: UpstreamDeployment[]
  replica_memory_mib_min: number | null
  context_length: number | null
  max_model_len: number | null
  kv_cache_dtype: string | null
  tool_call_parser: string | null
  reasoning_parser: string | null
  /** The whole tested command line, including flags nothing here has a field for. */
  args: string[]
  env: Record<string, unknown>
  features_on: string[]
  features_opt_in: string[]
  strategies: string[]
  requires_single_topology_domain: boolean
  verified_hardware: string[]
}

/** `ResolveNote`: `info` was handled; `warn` will bite (unaddressable weights, a gate). */
export interface ResolveNote {
  level: "info" | "warn"
  message: string
}

/** `ResolvedArtifact`: a source read as an artifact. `digest` comes from the file oids. */
export interface ResolvedArtifact {
  reference: string
  digest: string
  name: string
  kind: string
  format: string | null
  revision: string | null
  size_bytes: number
  licence: string | null
  parameter_count: number | null
  source_scheme: string
  source_uri: string
  verified: boolean
  metadata: Record<string, unknown>
  locations: ArtifactLocationRequest[]
  serving: ServingDefaults
  /** Null when no corpus entry names these weights. Never our own arithmetic. */
  upstream: UpstreamServing | null
  /** What the publisher claims; `upstream` is what an engine ran. */
  publisher: ArtifactUpstream
  existing_id: string | null
  notes: ResolveNote[]
}

/** `ArtifactRefresh`: `changed` lists dotted keys that moved; empty is a no-op success. */
export interface ArtifactRefresh {
  artifact: Artifact
  source: string
  changed: string[]
  notes: ResolveNote[]
}

/** The four kinds the plane accepts. Anything else is `422 invalid`. */
export const ARTIFACT_KINDS = [
  "adapter",
  "dataset",
  "image",
  "weights",
] as const

export interface ServingShape {
  device_class: string
  device_count: number
  device_memory_mib_min: number
  requires_single_topology_domain: boolean
  topology_domain_kind: string | null
}

export interface ServingParallelism {
  tensor: number
  pipeline: number
  expert: number
}

/** KV and prefix cache configuration. Unrelated to the weight cache. */
export interface ServingCache {
  kv_cache_dtype: string | null
  kv_cache_fraction: number | null
  prefix_cache_enabled: boolean
  prefix_cache_config: Record<string, unknown>
}

export interface ServingResources {
  cpu_cores: number | null
  memory_mib: number | null
  disk_gib: number | null
}

/** `ReasoningSchema`: flags that only mean something together. */
export interface ServingReasoning {
  default_on: boolean
  parser: string | null
  transport: string
  think_begin_token: string | null
  think_end_token: string | null
  suppress_eos_until_think_end: boolean
}

/** `ServingSchema`: API behavior as engine flags, so a change restarts replicas. */
export interface ServingBehavior {
  reasoning: ServingReasoning
  stream_usage: string
  context_overflow: string
  structured_output_backend: string | null
  tool_call_parser: string | null
  max_images_per_request: number | null
  max_videos_per_request: number | null
}

/** `CapabilitiesSchema`: claimed, never rendered, stored unchecked; conformance tests it. */
export interface ServingCapabilities {
  reasoning_supported: boolean
  streaming_supported: boolean
  structured_output_modes: string[]
  tool_calling_supported: boolean
  tool_choice_modes: string[]
  parallel_tool_calls: boolean
  input_modalities: string[]
  prompt_cache_mode: string
  prompt_cache_ttl_seconds: number | null
  asserted_by: string | null
  asserted_at: string | null
  source_uri: string | null
}

export const REASONING_TRANSPORTS = [
  "reasoning_content",
  "inline_tags",
] as const
export const STREAM_USAGE_MODES = [
  "final_chunk",
  "every_chunk",
  "on_request",
] as const
export const CONTEXT_OVERFLOW_MODES = [
  "reject",
  "truncate_input",
  "clamp_output",
] as const
export const STRUCTURED_OUTPUT_MODES = [
  "text",
  "json_object",
  "json_schema",
] as const
export const TOOL_CHOICE_MODES = [
  "auto",
  "none",
  "required",
  "named",
  "allowed_tools",
] as const
export const INPUT_MODALITIES = ["text", "image", "audio", "video"] as const
export const PROMPT_CACHE_MODES = ["off", "automatic", "explicit"] as const

/** Parsers we have run. The plane takes any string, so fields accept free text. */
export const REASONING_PARSERS = [
  "deepseek_r1",
  "glm45",
  "granite",
  "mistral",
  "qwen3",
] as const
export const TOOL_CALL_PARSERS = [
  "deepseek_v3",
  "granite",
  "hermes",
  "internlm",
  "llama3_json",
  "llama4_json",
  "mistral",
  "pythonic",
  "qwen3_coder",
] as const
export const STRUCTURED_OUTPUT_BACKENDS = [
  "guidance",
  "outlines",
  "xgrammar",
] as const

/** `OverlaySchema`: strategic merge over the pod; map merge over the graph deployment. */
export interface ServingOverlay {
  pod_spec: Record<string, unknown>
  dynamo: Record<string, unknown>
}

/**
 * `WorkloadServingSchema`: everything an engine is told, identified by `serving_digest`. The inner
 * `serving` is the wire name of the `ServingBehavior` group.
 */
export interface WorkloadServing {
  artifact_id: string
  engine: string
  engine_runtime_version: string
  engine_runtime_image: string | null
  /** A separately versioned frontend image. Null lets the cluster decide. */
  frontend_image: string | null
  engine_args: Record<string, unknown>
  /** Read off a running process. Recorded in full, never rendered, never hashed. */
  imported_args: Record<string, unknown>
  quantization: string | null
  dtype: string | null
  parallelism: ServingParallelism
  context_length: number
  max_batch_tokens: number | null
  cache: ServingCache
  serving: ServingBehavior
  /** Asserted, never rendered, and it never reaches a cluster. */
  capabilities: ServingCapabilities
  /** `engine_args` with the `serving` flags laid over, keyed without dashes. Derived on read. */
  resolved_engine_args: Record<string, string>
  shape: ServingShape
  resources: ServingResources
  overlay: ServingOverlay
  /** Absent from planes that predate disaggregation; read as `aggregated`. */
  topology?: ServingTopologyView
}

/** Write shape of `serving`, same as the read shape so a form round-trips. */
export interface WorkloadServingRequest {
  artifact: string
  engine: string
  engine_runtime_version: string
  context_length: number
  shape: ServingShape
  engine_runtime_image?: string | null
  frontend_image?: string | null
  engine_args?: Record<string, unknown>
  imported_args?: Record<string, unknown>
  quantization?: string | null
  dtype?: string | null
  parallelism?: ServingParallelism
  max_batch_tokens?: number | null
  cache?: ServingCache
  resources?: ServingResources
  overlay?: ServingOverlay
  serving?: ServingBehavior
  capabilities?: ServingCapabilities
  /** Omitted when aggregated, so existing digests do not change. */
  topology?: ServingTopologyRequest
}

/** A partial update without the artifact. Each sub-block replaces whole. */
export type WorkloadServingUpdate = Partial<
  Omit<WorkloadServingRequest, "artifact">
>

/**
 * `WorkloadRevisionSchema`: a held configuration, stored as the PATCH body that restores it.
 * `number` never rewinds; going back appends.
 */
export interface WorkloadRevision {
  id: string
  number: number
  serving_digest: string
  artifact: string
  reason: WorkloadRevisionReason
  /** Why, in somebody's words. The only mutable thing about a revision. */
  note: string | null
  created_at: string
  serving: WorkloadServingUpdate
}

/** `imported` records what a workload ran before it had a history. */
export type WorkloadRevisionReason =
  "created" | "updated" | "reshaped" | "promoted" | "imported" | (string & {})

/** The only write a revision accepts. `null` clears the note. */
export interface WorkloadRevisionUpdate {
  note: string | null
}

/** `Workload.kind` — one scheduling supertype across serving and batch. */
export type WorkloadKind = "service" | "task" | "gang" | (string & {})

/** Derived: `running` when all run, `failed` if any failed, else the least advanced. */
export type WorkloadPhase =
  "pending" | "admitted" | "placed" | "running" | "failed" | (string & {})

/** `pack` fills one cluster before the next; `spread` levels them. */
export type WorkloadStrategy = "pack" | "spread" | (string & {})

/** `DeploymentReplicaRollupSchema` — observed replicas, counted not listed. */
export interface DeploymentReplicaRollup {
  total: number
  ready: number
  running: number
  pending: number
  failed: number
  at_current_generation: number
}

/** `WorkloadScaleSchema`: declared totals in `unit`, plus sums over the deployments. */
export interface WorkloadScale {
  unit: string
  min: number
  max: number
  desired: number
  placed: number
  ready: number
}

/** `WorkloadDeploymentSchema`: one cluster's share, for the per-cluster table. */
export interface WorkloadDeployment {
  id: string
  name: string
  cluster_id: string
  cluster_name: string
  region: string | null
  min_replicas: number
  max_replicas: number
  desired_replicas: number
  admission_phase: string
  admission_reason: string | null
  generation: number
  enabled: boolean
  authority: DeploymentAuthority
  queued_at: string | null
  admitted_at: string | null
  placed_at: string | null
  replicas: DeploymentReplicaRollup
  status: DeploymentStatus | null
  /** Ready replicas per digest. Two entries is a rollout; empty is unreported, not none. */
  live_digests: Record<string, number>
}

/** `WorkloadSchema`: global. A gang or pinned workload has exactly one deployment. */
export interface Workload {
  id: string
  name: string
  kind: WorkloadKind
  phase: WorkloadPhase
  project_id: string
  priority_class: string
  preemptible: boolean
  paused: boolean
  gang_size: number
  /** `svg_<64 hex>` over what reaches an engine, computed by the plane on write. */
  serving_digest: string
  serving: WorkloadServing
  adapter_artifact_ids: string[]
  /** Registered model id, from the artifact's slug. Clients use the endpoint's `slug`. */
  served_model_name: string
  overrides: Record<string, unknown>
  labels: Record<string, string>
  strategy: WorkloadStrategy
  /** `{cluster_uid: minimum share}`. An unknown uid is ignored, never an error. */
  cluster_floors: Record<string, number>
  max_clusters: number | null
  /** The deliberate scheduler escape: checked for existence, never for capacity. */
  pinned_cluster_id: string | null
  scale: WorkloadScale
  started_at: string | null
  finished_at: string | null
  deadline_at: string | null
  created_at: string
  updated_at: string | null
  revision: string
  deployments: WorkloadDeployment[]
  /** The artifact's tag, resolved by the plane so a list needn't fetch each one. */
  artifact_tag: string | null
}

/** `WorkloadPlacementNodeSchema`: a node holding live devices of this workload. */
export interface WorkloadPlacementNode {
  node: string
  cluster: string
  device_count: number
}

/** `WorkloadReplicaPlacementSchema`: one live allocation; `replica` null until tied. */
export interface WorkloadReplicaPlacement {
  allocation: string
  replica: string | null
  cluster: string
  nodes: string[]
  devices: string[]
  device_count: number
  gang_complete: boolean
}

/** `WorkloadPlacementSchema`: from live allocations at read time. */
export interface WorkloadPlacement {
  workload: string
  node_count: number
  gpu_count: number
  nodes: WorkloadPlacementNode[]
  replicas: WorkloadReplicaPlacement[]
}

/** Scale numbers are totals across clusters; `cluster` forces a single deployment. */
export interface WorkloadCreateRequest {
  serving: WorkloadServingRequest
  name: string
  project: string
  cluster?: string | null
  /** Narrows placement: the plane stores either list as a Policy on the workload. */
  allowed_regions?: string[]
  allowed_clusters?: string[]
  priority_class?: string | null
  kind?: WorkloadKind
  min_scale?: number
  max_scale?: number
  desired_scale?: number
  strategy?: WorkloadStrategy
  cluster_floors?: Record<string, number>
  max_clusters?: number | null
  enabled?: boolean
  preemptible?: boolean
  gang_size?: number
  deadline_at?: string | null
  adapter_artifacts?: string[]
  overrides?: Record<string, unknown>
  labels?: Record<string, string>
  /** Why this configuration, landing on the revision this create files. */
  note?: string | null
}

/** No cluster or scale (scale is the `workload.scale` operation). `serving` rolls replicas. */
export interface WorkloadUpdateRequest {
  name?: string
  adapter_artifacts?: string[]
  serving?: WorkloadServingUpdate
  overrides?: Record<string, unknown>
  labels?: Record<string, string>
  priority_class?: string
  preemptible?: boolean
  deadline_at?: string | null
  strategy?: WorkloadStrategy
  cluster_floors?: Record<string, number>
  max_clusters?: number | null
  /** A patch that files no revision puts the note on the one in force. */
  note?: string | null
}

/** One cluster's share; replica counts are the share, never the total. */
export interface Deployment {
  id: string
  name: string
  workload_id: string
  workload_name: string
  cluster_id: string
  min_replicas: number
  max_replicas: number
  desired_replicas: number
  admission_phase: string
  admission_reason: string | null
  queued_at: string | null
  admitted_at: string | null
  placed_at: string | null
  generation: number
  enabled: boolean
  created_at: string
  updated_at: string | null
  revision: string
  /** The *discovered* workload this was promoted from. Unrelated to `workload_id`. */
  adopted_workload_id: string | null
  adopted_fingerprint: string | null
  /** Who owns this: `observed` records and verifies, `managed` renders and converges. */
  authority: DeploymentAuthority
  replicas: DeploymentReplicaRollup
  /** What the cluster reports it is doing. Null until a cluster has reported at all. */
  status: DeploymentStatus | null
}

/** Observed. `reason` is an expected wait, `last_error_*` a failure. Unknown reasons show. */
export interface DeploymentStatus {
  observed_generation: number
  phase: string
  reason: string | null
  message: string | null
  desired_replicas: number
  ready_replicas: number
  updated_replicas: number
  unavailable_replicas: number
  /** Per-role counts, not served by any plane yet (see `DeploymentRoleObserved`). */
  roles?: DeploymentRoleObserved[]
  last_error_code: string | null
  last_error_message: string | null
  last_error_at: string | null
  observed_at: string | null
}

/** No partial ownership (ADR 0009). */
export type DeploymentAuthority = "observed" | "managed"

/** `EndpointTargetSchema`. Named by deployment id; the cluster is not repeated. */
export interface EndpointTarget {
  deployment_id: string
  weight: number
  role: string
  enabled: boolean
  /** Override for the engine's model name. Costs a gateway poll, not a reload. */
  served_model_name: string | null
}

/** `EndpointSurfaceSchema`: gateway terms, enforced or only disclosed (`SURFACE_ENFORCEMENT`). */
export interface EndpointSurface {
  protocols: string[]
  auth_mode: string | null
  error_shape: string | null
  max_request_bytes: number | null
  request_timeout_seconds: number | null
  streaming_transport: string | null
  cors_enabled: boolean | null
  /** Output-length request fields in precedence order; the quota applies to the first. */
  output_length_params: string[]
  /** Terms enforced on top of the engine. Null forwards requests as given. */
  policy?: EndpointPolicy | null
}

/**
 * `RuleSchema`: scope (`route`, `stream`, `when_present`), subject (`key` or `predicate`), then
 * terms that bound, deny or rewrite.
 */
export interface PolicyRule {
  route?: string
  stream?: boolean | null
  when_present?: string
  key?: string
  predicate?: string
  enum?: unknown[] | null
  min?: number | null
  max?: number | null
  deny?: boolean
  drop?: boolean
  default?: unknown
  value_from?: string
}

/** `PolicySchema`: `version` 2 is scoped rules; the plane converts v1 on read. */
export interface EndpointPolicy {
  version: number
  rules: PolicyRule[]
}

/** `EndpointSchema`: the stable public name. Global; no cluster at any depth. */
export interface Endpoint {
  id: string
  name: string
  /** The model id clients send. Defaults from the first target's artifact. */
  slug: string
  /** Other ids that reach this endpoint; one namespace with all slugs. Records key on `slug`. */
  aliases: string[]
  project_id: string
  visibility: string
  routing_strategy: string
  enabled: boolean
  /** Absent on sparse payloads: an endpoint may arrive with no surface declared. */
  surface?: EndpointSurface
  targets: EndpointTarget[]
  failover_order: string[]
  labels: Record<string, string>
  created_at: string
  updated_at: string | null
  revision: string
}

/** `EndpointTargetRequest` — names the deployment, never the cluster. */
export interface EndpointTargetRequest {
  deployment: string
  weight?: number
  role?: string
  enabled?: boolean
  served_model_name?: string | null
}

export interface EndpointCreateRequest {
  name: string
  project: string
  /** Empty takes the slug of the weights behind the first target. Give one only for an alias. */
  slug?: string
  aliases?: string[]
  visibility?: string
  routing_strategy?: string
  enabled?: boolean
  targets?: EndpointTargetRequest[]
  failover_order?: string[]
  labels?: Record<string, string>
}

export interface EndpointUpdateRequest {
  slug?: string
  /** Replaces the whole set. Omitting it leaves the aliases alone; `[]` retires every one. */
  aliases?: string[]
  visibility?: string
  routing_strategy?: string
  enabled?: boolean
  surface?: EndpointSurface
  targets?: EndpointTargetRequest[]
  failover_order?: string[]
  labels?: Record<string, string>
}

/** `topology` on the wire, `serving_topology` in the column. */
export type ServingTopology = "aggregated" | "disaggregated" | (string & {})

/** The roles a disaggregated workload has, and it has both or it is not one. */
export const SERVING_ROLES = ["prefill", "decode"] as const
export type ServingRole = (typeof SERVING_ROLES)[number] | (string & {})

/**
 * One role. `weight` is scale, outside the digest; the rest is in it and reloads weights. A null
 * `max_batch_tokens` inherits the workload's.
 */
export interface WorkloadRole {
  role: ServingRole
  weight: number
  engine_args: Record<string, unknown>
  max_batch_tokens: number | null
}

/** The write shape of a role. Absent keys leave the stored value alone. */
export interface WorkloadRoleRequest {
  role: ServingRole
  weight?: number
  engine_args?: Record<string, unknown>
  max_batch_tokens?: number | null
}

/** `TopologySchema`: the write shape. Aggregated is expressed by omitting it. */
export interface ServingTopologyRequest {
  serving_topology: ServingTopology
  roles?: WorkloadRoleRequest[]
}

/** `TopologyViewSchema`: `group_devices` is derived on read, keeping weights out of the digest. */
export interface ServingTopologyView {
  serving_topology: ServingTopology
  roles: WorkloadRole[]
  group_devices: number
}

/**
 * One role's observed counts. No plane serves this yet, so readers fall back to the scalar rollup.
 */
export interface DeploymentRoleObserved {
  role: ServingRole
  desired: number
  ready: number
}
