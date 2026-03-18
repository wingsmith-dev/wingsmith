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

/** `Workload.type` — one scheduling supertype across serving and batch. */
export type WorkloadType = "service" | "task" | "gang" | (string & {})

/** `Workload.phase`. Decided by placement, never asserted by a client. */
export type WorkloadPhase =
  | "pending"
  | "admitted"
  | "placed"
  | "running"
  | (string & {})

/**
 * `DeploymentWorkloadSchema` — the supertype half. Not copied onto the
 * deployment, so preemption and quota stay one code path.
 */
export interface DeploymentWorkload {
  type: WorkloadType
  phase: WorkloadPhase
  reason: string | null
  project_id: string
  priority_class: string
  preemptible: boolean
  gang_size: number
  queued_at: string | null
  admitted_at: string | null
  placed_at: string | null
  started_at: string | null
  deadline_at: string | null
}

/** `DeploymentReplicaRollupSchema` — observed replicas, counted not listed. */
export interface DeploymentReplicaRollup {
  total: number
  ready: number
  running: number
  pending: number
  failed: number
  at_current_generation: number
}

export interface Deployment {
  id: string
  name: string
  cluster_id: string
  recipe_id: string
  min_replicas: number
  max_replicas: number
  desired_replicas: number
  generation: number
  enabled: boolean
  adapter_artifact_ids: string[]
  overrides: Record<string, unknown>
  labels: Record<string, string>
  created_at: string
  updated_at: string | null
  revision: string
  workload: DeploymentWorkload
  replicas: DeploymentReplicaRollup
}

export interface DeploymentUpdateRequest {
  name?: string
  min_replicas?: number
  max_replicas?: number
  desired_replicas?: number
  enabled?: boolean
  adapter_artifacts?: string[]
  overrides?: Record<string, unknown>
  labels?: Record<string, string>
  priority_class?: string
  preemptible?: boolean
  deadline_at?: string | null
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

export interface EndpointUpdateRequest {
  display_name?: string | null
  visibility?: string
  routing_strategy?: string
  enabled?: boolean
  targets?: { deployment: string; weight?: number; role?: string; enabled?: boolean }[]
  failover_order?: string[]
  labels?: Record<string, string>
}
