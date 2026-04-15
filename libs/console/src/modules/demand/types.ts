/** `ArtifactLocationSchema`. No id: `uri` is unique within the artifact. */
export interface ArtifactLocation {
  scheme: string
  uri: string
  region: string | null
  preferred: boolean
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
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string | null
  revision: string
  locations: ArtifactLocation[]
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
  metadata?: Record<string, unknown>
  locations?: ArtifactLocationRequest[]
}

/**
 * `RecipeDefaults` — serving parameters implied by the weights. A suggestion the
 * plane never applies: a recipe is declared state someone owns, so these are
 * carried into the recipe form and nowhere else.
 */
export interface RecipeDefaults {
  device_count: number
  device_memory_mib_min: number
  context_length: number | null
  quantization: string | null
  dtype: string | null
}

/**
 * `ResolvedArtifact` — a source manifest read as an artifact. Nothing is written.
 * `digest` is derived by the plane from the source's per-file oids, which is why
 * the form shows it read-only: a digest a person typed is a claim nobody checked.
 */
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
  recipe: RecipeDefaults
  existing_id: string | null
  notes: string[]
}

/** The four kinds the plane accepts. Anything else is `409 invalid`. */
export const ARTIFACT_KINDS = [
  "adapter",
  "dataset",
  "image",
  "weights",
] as const

export interface RecipeShape {
  device_class: string
  device_count: number
  device_memory_mib_min: number
  requires_single_topology_domain: boolean
  topology_domain_kind: string | null
}

export interface RecipeParallelism {
  tensor: number
  pipeline: number
  expert: number
}

/** KV and prefix cache configuration. Unrelated to the weight cache. */
export interface RecipeCache {
  kv_cache_dtype: string | null
  kv_cache_fraction: number | null
  prefix_cache_enabled: boolean
  prefix_cache_config: Record<string, unknown>
}

export interface RecipeResources {
  cpu_cores: number | null
  memory_mib: number | null
  disk_gib: number | null
}

export interface Recipe {
  id: string
  artifact_id: string
  name: string
  version: number
  engine: string
  engine_version: string
  engine_image: string | null
  engine_args: Record<string, unknown>
  /** Read off a running process. Recorded, never rendered, never enforced. */
  imported_args: Record<string, unknown>
  quantization: string | null
  dtype: string | null
  parallelism: RecipeParallelism
  context_length: number
  max_batch_tokens: number | null
  cache: RecipeCache
  shape: RecipeShape
  resources: RecipeResources
  labels: Record<string, string>
  created_at: string
  updated_at: string | null
  revision: string
}

/**
 * The write shape is flat where the read shape is nested — parallelism, cache,
 * shape and resources are top-level fields on the request and grouped objects
 * on the response.
 */
export interface RecipeCreateRequest {
  artifact: string
  name: string
  engine: string
  engine_version: string
  device_class: string
  context_length: number
  version?: number
  engine_image?: string | null
  engine_args?: Record<string, unknown>
  quantization?: string | null
  dtype?: string | null
  tensor_parallel?: number
  pipeline_parallel?: number
  expert_parallel?: number
  max_batch_tokens?: number | null
  kv_cache_dtype?: string | null
  kv_cache_fraction?: number | null
  prefix_cache_enabled?: boolean
  prefix_cache_config?: Record<string, unknown>
  device_count?: number
  device_memory_mib_min?: number
  requires_single_topology_domain?: boolean
  topology_domain_kind?: string | null
  cpu_cores?: number | null
  memory_mib?: number | null
  disk_gib?: number | null
  labels?: Record<string, string>
}

/** The artifact is absent: repointing a recipe at other bytes is a new recipe. */
export interface RecipeUpdateRequest {
  name?: string
  engine?: string
  engine_version?: string
  engine_image?: string | null
  engine_args?: Record<string, unknown>
  quantization?: string | null
  dtype?: string | null
  tensor_parallel?: number
  pipeline_parallel?: number
  expert_parallel?: number
  context_length?: number
  max_batch_tokens?: number | null
  kv_cache_dtype?: string | null
  kv_cache_fraction?: number | null
  prefix_cache_enabled?: boolean
  prefix_cache_config?: Record<string, unknown>
  device_class?: string
  device_count?: number
  device_memory_mib_min?: number
  requires_single_topology_domain?: boolean
  topology_domain_kind?: string | null
  cpu_cores?: number | null
  memory_mib?: number | null
  disk_gib?: number | null
  labels?: Record<string, string>
}

/** `Workload.kind` — one scheduling supertype across serving and batch. */
export type WorkloadKind = "service" | "task" | "gang" | (string & {})

/**
 * `Workload.phase`, derived from the deployments underneath it and stored
 * nowhere: `running` when every deployment runs, `failed` if any failed,
 * otherwise the least advanced phase among them.
 */
export type WorkloadPhase =
  | "pending"
  | "admitted"
  | "placed"
  | "running"
  | "failed"
  | (string & {})

/**
 * How placement spends the total across the clusters a workload may use.
 * `pack` fills one cluster before reaching for the next; `spread` levels them.
 */
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

/**
 * `WorkloadScaleSchema` — declared totals, and what the shares add up to.
 *
 * `unit` is named by kind — replicas, ranks, completions — because the three
 * numbers count different things and a bare integer is unreadable without it.
 * `placed` and `ready` are sums over the deployments and are the only place
 * summing a share against anything is legal.
 */
export interface WorkloadScale {
  unit: string
  min: number
  max: number
  desired: number
  placed: number
  ready: number
}

/**
 * `WorkloadDeploymentSchema` — one cluster's share, as seen from the workload.
 *
 * Enough to render the per-cluster table and link into the deployment; the full
 * per-cluster object is `GET /deployments/{ref}`.
 */
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
}

/**
 * `WorkloadSchema` — what was asked for, and where it ended up.
 *
 * The workload is global: it names no cluster. `deployments` is the per-cluster
 * expansion placement chose, one row per cluster. A `gang` always has exactly
 * one — splitting a gang across clusters is rejected at placement, not ranked
 * down — and so does a workload with `pinned_cluster_id` set.
 */
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
  recipe_id: string
  adapter_artifact_ids: string[]
  overrides: Record<string, unknown>
  labels: Record<string, string>
  /**
   * The workload's own topology requirement, overriding the recipe's when set.
   * Null means the recipe's answer stands, not that there is no requirement.
   */
  requires_single_topology_domain: boolean | null
  topology_domain_kind: string | null
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
}

/**
 * What it takes to run something, without saying where.
 *
 * `cluster` is optional and absent is the normal case: placement picks from the
 * recipe's shape, the project's quota, the policies on the workload and what the
 * clusters report free. Naming one is an override that forces a single deployment.
 *
 * The three scale numbers are TOTALS across every cluster this workload may use.
 */
export interface WorkloadCreateRequest {
  recipe: string
  name: string
  project: string
  cluster?: string | null
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
  requires_single_topology_domain?: boolean | null
  topology_domain_kind?: string | null
}

/**
 * A partial update. Recipe and cluster are absent by design — a deployment is
 * recipe x cluster, so changing either is a new one. Scale is absent too: it is
 * long-running and can sit pending on quota forever, so it is the
 * `workload.scale` operation and not a field write.
 */
export interface WorkloadUpdateRequest {
  name?: string
  adapter_artifacts?: string[]
  overrides?: Record<string, unknown>
  labels?: Record<string, string>
  priority_class?: string
  preemptible?: boolean
  deadline_at?: string | null
  strategy?: WorkloadStrategy
  cluster_floors?: Record<string, number>
  max_clusters?: number | null
  requires_single_topology_domain?: boolean | null
  topology_domain_kind?: string | null
}

/**
 * One workload's share on one cluster: declared spec, admission, observed replicas.
 *
 * `min_replicas` / `max_replicas` / `desired_replicas` are THIS CLUSTER'S SHARE of
 * the workload's total, never the total. Summing a share against a total, or
 * dividing one out of the other, is always a bug. Placement sets all three equal,
 * so there is no range here to render.
 *
 * Recipe, adapters, overrides and labels are not here: they are workload facts,
 * read through `workload_id`. Admission is here, because it is per-cluster — a
 * workload can be running in one region and queued on quota in another.
 */
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

/**
 * The observed half: where the deployment is, and what is holding it there.
 *
 * `reason` and `message` are deliberately not errors. Most of a deployment's life
 * before it serves is spent waiting on an ordering — the recipe has to arrive, the
 * weights have to be resident, a node has to advertise enough accelerators — and
 * every one of those is expected. `last_error_*` is for the things that went wrong.
 *
 * `reason` is a closed set the operator owns and is typed as a string on purpose: a
 * cluster runs its own build and may report a sub-state this console has never heard
 * of, and showing it beats hiding it.
 */
export interface DeploymentStatus {
  observed_generation: number
  phase: string
  reason: string | null
  message: string | null
  desired_replicas: number
  ready_replicas: number
  updated_replicas: number
  unavailable_replicas: number
  last_error_code: string | null
  last_error_message: string | null
  last_error_at: string | null
  observed_at: string | null
}

/**
 * The two answers to who owns a deployment, and there is deliberately no third.
 *
 * A deployment with some fields enforced and others drifting cannot be reasoned
 * about at the moment somebody needs to (ADR 0009).
 */
export type DeploymentAuthority = "observed" | "managed"

/** Taking management, or giving it back. `fingerprint` is required only when taking it. */
export interface DeploymentAuthorityRequest {
  authority: DeploymentAuthority
  fingerprint?: string | null
}

/**
 * A partial update of one cluster's copy.
 *
 * Everything a workload declares is absent: it is declared once, globally, and a
 * per-cluster override would be a second answer to a question that already has one.
 * Scale is absent because shares are derived by placement from the workload total,
 * not asserted per cluster.
 */
export interface DeploymentUpdateRequest {
  enabled?: boolean
}

/** `EndpointTargetSchema`. Named by deployment id; the cluster is not repeated. */
export interface EndpointTarget {
  deployment_id: string
  weight: number
  role: string
  enabled: boolean
}

/**
 * `EndpointSchema` — the stable public name clients call. Global and
 * plane-only: there is no cluster on this object at any depth, because an
 * endpoint spans clusters by construction.
 */
export interface Endpoint {
  id: string
  name: string
  display_name: string | null
  project_id: string
  visibility: string
  routing_strategy: string
  enabled: boolean
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
}

export interface EndpointCreateRequest {
  name: string
  project: string
  display_name?: string | null
  visibility?: string
  routing_strategy?: string
  enabled?: boolean
  targets?: EndpointTargetRequest[]
  failover_order?: string[]
  labels?: Record<string, string>
}

export interface EndpointUpdateRequest {
  display_name?: string | null
  visibility?: string
  routing_strategy?: string
  enabled?: boolean
  targets?: EndpointTargetRequest[]
  failover_order?: string[]
  labels?: Record<string, string>
}
