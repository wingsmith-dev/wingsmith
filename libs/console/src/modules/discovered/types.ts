/**
 * A workload the cluster was already running when we arrived. The sighting is
 * observed — ingestion is its only writer — and the decision we record beside it
 * is ours. The two are joined on read, which is why `status` is on this object
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
  project_id: string | null
  note: string | null
  decided_by: string | null
  decided_at: string | null
  first_seen_at: string | null
  observed_at: string | null
}

export type DiscoveryStatus = "new" | "tracked" | "ignored" | "promoted"

export const DISCOVERY_STATUSES: DiscoveryStatus[] = [
  "new",
  "tracked",
  "ignored",
  "promoted",
]

/** Tracking and ignoring record a decision. Neither reaches a cluster. */
export interface DecisionRequest {
  project?: string | null
  note?: string | null
}

/**
 * `fingerprint` is the whole safety of a promotion: it is `If-Match` for a spec
 * the plane does not own. Scale is deliberately absent — the bounds come from
 * what is already running, so cataloguing a workload never rescales it.
 */
export interface PromoteRequest {
  recipe: string
  project: string
  fingerprint: string
  name?: string | null
  priority_class?: string | null
}
