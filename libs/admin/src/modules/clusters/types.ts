export type ClusterPhase = "pending" | "active" | "draining" | "disabled"

export type SyncState = "connected" | "reconnecting" | "disconnected"

export type ComponentPhase = "healthy" | "upgrading" | "degraded" | "missing"

/** Observed state of the operator's outbound sync stream. */
export interface ClusterSync {
  state: SyncState
  operator_version: string
  connected_since: string | null
  last_heartbeat_at: string | null
  last_sync_at: string | null
  stream_lag_ms: number | null
  pending_declarations: number
  last_error: string | null
}

/** Denormalised counters rolled up from observed Node and Device records. */
export interface ClusterCapacity {
  nodes_total: number
  nodes_ready: number
  devices_total: number
  devices_healthy: number
  devices_allocated: number
}

export interface Cluster {
  id: string
  name: string
  display_name: string
  region: string
  phase: ClusterPhase
  /**
   * Observed, and deliberately not a phase: a cluster can be `active` and
   * unreachable at the same time, which is exactly the state worth seeing.
   */
  reachable: boolean
  labels: Record<string, string>
  device_classes: string[]
  capacity: ClusterCapacity
  sync: ClusterSync
  enrolled_at: string
  created_at: string
  updated_at: string | null
}

export interface ClusterComponent {
  name: string
  kind: "operator" | "runtime" | "cache" | "gateway" | "exporter" | "collector"
  declared_version: string
  observed_version: string | null
  phase: ComponentPhase
  last_updated_at: string | null
}

export interface ClusterDetail extends Cluster {
  topology_domain_count: number
  components: ClusterComponent[]
}
