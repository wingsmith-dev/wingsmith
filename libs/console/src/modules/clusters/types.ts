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

/**
 * A one-time bootstrap command that installs the operator into a cluster. The
 * `command` carries a short-lived, single-cluster token and no secret — the
 * runtime credential is injected into the manifest the command fetches.
 */
export interface ClusterInstall {
  command: string
  manifest_url: string
  expires_in_seconds: number
}

/**
 * How much of a cluster is free to place onto, derived at read time from
 * observed devices — nothing here is stored. `largest_contiguous_block` is the
 * biggest set of allocatable devices sharing one topology domain (what a job
 * wanting one fabric can actually get), distinct from `free_devices` which
 * counts every allocatable device regardless of where it sits.
 * `whole_nodes_free` counts nodes on which every device is allocatable.
 */
export interface ClusterHeadroom {
  free_devices: number
  whole_nodes_free: number
  largest_contiguous_block: number
}

/**
 * A cluster's observed footprint in aggregate, for the list and card views.
 * Every field is derived at read time. `devices_allocated` is
 * `devices_total - devices_allocatable`; `pool_count` and `deployment_count`
 * are the declared objects hanging off the cluster, the rest are observed.
 */
export interface ClusterRollup {
  cluster_id: string
  nodes_total: number
  nodes_ready: number
  devices_total: number
  devices_allocatable: number
  devices_allocated: number
  pool_count: number
  deployment_count: number
  headroom: ClusterHeadroom
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
