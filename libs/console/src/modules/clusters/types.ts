/**
 * `ClusterSchema.phase` — declared. What we asked for, and never where
 * reachability lives: folding "the network is down" into the same field as
 * "somebody disabled this cluster" makes two situations that need different
 * responses indistinguishable.
 */
export type ClusterPhase = "pending" | "active" | "draining" | "disabled"

/**
 * `ClusterSyncSchema.state` — observed and derived, never stored. Left open
 * because the plane derives this value and may derive one the console has not
 * been taught about yet.
 */
export type SyncState =
  | "connected"
  | "disconnected"
  | "never_connected"
  /** Link up, cluster silent. Reachable says nothing about whether it still reports. */
  | "stale"
  | (string & {})

/** Observed liveness of a cluster's agent link. */
export interface ClusterSync {
  state: SyncState
  last_sync_at: string | null
  last_seen_at: string | null
  agent_version: string | null
  lag_ms: number | null
  last_error: string | null
  /** What the cluster turned out to be, as opposed to how it was enrolled. */
  distribution: string | null
  cloud: string | null
  observed_region: string | null
}

/**
 * Where the data plane reaches this cluster's serving runtime. Declared, and
 * deliberately not part of the cluster patch body: the plane keeps it as its own
 * sub-resource because nothing in the control plane ever dials it — only the
 * gateway does.
 */
export interface ClusterServing {
  /** Opaque to hangar: a public ingress, an in-cluster service name, anything dialable. */
  url: string
  /** A reference to whatever fronts the address, never the secret itself. */
  credential_id: string | null
  enabled: boolean
}

export interface ClusterServingRequest {
  url: string
  credential_id?: string | null
  enabled?: boolean
}

export interface Cluster {
  id: string
  name: string
  display_name: string | null
  region: string
  zone: string | null
  residency: string | null
  phase: ClusterPhase
  labels: Record<string, string>
  capabilities: Record<string, unknown>
  enrolled_at: string | null
  created_at: string
  updated_at: string | null
  revision: string
  /** Observed. A cluster can be `active` and unreachable at the same time. */
  reachable: boolean
  sync: ClusterSync
  /** Null until an address is declared. A cluster is schedulable without one. */
  serving: ClusterServing | null
}

export interface ClusterCreateRequest {
  name: string
  region: string
  display_name?: string | null
  zone?: string | null
  residency?: string | null
  phase?: ClusterPhase
  labels?: Record<string, string>
  capabilities?: Record<string, unknown>
}

export interface ClusterUpdateRequest {
  display_name?: string | null
  region?: string
  zone?: string | null
  residency?: string | null
  phase?: ClusterPhase
  labels?: Record<string, string>
  capabilities?: Record<string, unknown>
}
