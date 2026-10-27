/**
 * A workload the cluster was already running when it was connected. The sighting
 * is observed — ingestion is its only writer — and the decision recorded beside it
 * is the operator's. The two are joined on read, which is why `status` is on this object
 * but nothing writes it directly.
 */
export interface DiscoveredWorkload {
  id: string
  cluster_id: string
  cluster_name: string
  namespace: string
  name: string
  kind: string
  model: string | null
  images: string[]
  replicas_desired: number
  replicas_ready: number
  devices_per_replica: number
  devices_total: number
  node_ids: string[]
  labels: Record<string, string>
  status: DiscoveryStatus
  deployment_id: string | null
  spec_fingerprint: string | null
  spec: WorkloadSpec
  project_id: string | null
  note: string | null
  decided_by: string | null
  decided_at: string | null
  first_seen_at: string | null
  observed_at: string | null
}

export type DiscoveryStatus =
  "new" | "tracked" | "ignored" | "dismissed" | "promoted"

export const DISCOVERY_STATUSES: DiscoveryStatus[] = [
  "new",
  "tracked",
  "ignored",
  "dismissed",
  "promoted",
]

/**
 * Tracking, ignoring and removing record a decision. None reaches a cluster.
 * A removed sighting is filtered out of the default listing rather than deleted:
 * the row is observed and ingestion goes on refreshing it, which is what makes
 * removal undoable.
 */
export interface DecisionRequest {
  project?: string | null
  note?: string | null
}

/**
 * `fingerprint` is the whole safety of a promotion: it is `If-Match` for a spec
 * the plane does not own. Scale is deliberately absent — the bounds come from
 * what is already running, so cataloging a workload never rescales it.
 *
 * `artifact` is the only part of the serving configuration a caller states,
 * because it is the only part the cluster cannot tell us. Everything else is read
 * off the sighting — the same reading `GET /serving-draft` returns — so that what
 * is declared is what is running. Submitting a serving block here would declare a
 * configuration for a process that is already up, and the first reconcile would
 * either refuse it or restart the model, which is the one outcome adoption exists
 * to avoid. Editing it is an ordinary `PATCH /workloads/{ref}` afterwards, where a
 * restart is what the caller asked for.
 */
export interface PromoteRequest {
  artifact: string
  project: string
  fingerprint: string
  name?: string | null
  priority_class?: string | null
}

/**
 * The weights a sighting is serving, as far as they can be identified.
 *
 * `digest` is the reference — `hf:org/model`, with the revision when the args
 * carry one — and `verified` is false to say so: a cluster reports what it was
 * told to load, never what it loaded. `existing_id` is the catalog's id for
 * these weights when it already holds them, which is what makes the first step
 * of adoption a confirmation rather than a duplicate.
 */
export interface ArtifactDraft {
  reference: string | null
  digest: string | null
  name: string | null
  kind: string
  format: string | null
  revision: string | null
  parameter_count: number | null
  source_scheme: string | null
  source_uri: string | null
  verified: boolean
  metadata: Record<string, unknown>
  existing_id: string | null
  notes: string[]
}

/**
 * A serving block proposed from what a cluster reported about a running workload.
 *
 * `confident` is false when the engine could not be identified, which makes every
 * field below it a guess rather than a reading; `notes` names what could not be
 * determined, so the person confirming knows where to look.
 */
export interface ServingDraft {
  artifact_ref: string | null
  artifact_name: string | null
  name: string
  engine: string | null
  engine_runtime_version: string | null
  engine_runtime_image: string | null
  engine_args: Record<string, string>
  /** The whole argv, including what also populated a field above. Always a superset. */
  imported_args: Record<string, string>
  context_length: number | null
  tensor_parallel: number
  pipeline_parallel: number
  expert_parallel: number
  max_batch_tokens: number | null
  kv_cache_fraction: number | null
  quantization: string | null
  dtype: string | null
  device_count: number
  device_class: string | null
  cpu_cores: number | null
  memory_mib: number | null
  confident: boolean
  notes: string[]
  artifact: ArtifactDraft
}

/** How the workload was told to serve, as its own cluster records it. */
export interface WorkloadSpec {
  image?: string | null
  command?: string[]
  args?: string[]
  env_names?: string[]
  requests?: Record<string, string>
  limits?: Record<string, string>
  node_selector?: Record<string, string>
}
