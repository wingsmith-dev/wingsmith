export type NodeCondition = "ready" | "not_ready" | "unknown"

export type DeviceHealth = "healthy" | "degraded" | "failed" | "unknown"

export type TopologyKind = "nvswitch" | "nvlink" | "pcie" | "numa"

export type ProvisioningSource =
  | "owned_bare_metal"
  | "colocation"
  | "cloud_reserved"
  | "cloud_on_demand"

export interface EccCounters {
  volatile: number
  aggregate: number
}

/** Which replica currently holds the device, and who that replica belongs to. */
export interface DeviceAllocation {
  replica_id: string
  deployment_id: string
  deployment_name: string
  project_id: string
  project_name: string
  allocated_at: string
}

export interface TopologyDomain {
  id: string
  cluster_id: string
  cluster_name: string
  node_id: string
  node_name: string
  kind: TopologyKind
  interconnect: string
  bandwidth_gbps: number | null
  device_count: number
  devices_healthy: number
  devices_allocated: number
}

export interface SupplyNode {
  id: string
  cluster_id: string
  cluster_name: string
  name: string
  pool_id: string | null
  pool_name: string | null
  condition: NodeCondition
  /**
   * Observed schedulability. Distinct from `condition`: a node is cordoned by a
   * drain Operation and stays cordoned long after it reports ready again.
   */
  cordoned: boolean
  instance_type: string
  kernel_version: string | null
  driver_version: string | null
  cpu_cores: number
  memory_gb: number
  device_class: string | null
  device_count: number
  devices_healthy: number
  devices_allocated: number
  topology_domain_ids: string[]
  labels: Record<string, string>
  taints: string[]
  joined_at: string
  last_seen_at: string | null
}

export interface NodeDetail extends SupplyNode {
  topology_domains: TopologyDomain[]
}

export interface Device {
  id: string
  cluster_id: string
  cluster_name: string
  node_id: string
  node_name: string
  index: number
  uuid: string
  device_class: string
  memory_gb: number
  memory_used_gb: number
  health: DeviceHealth
  throttled: boolean
  throttle_reasons: string[]
  ecc_errors: EccCounters
  mig_profile: string | null
  topology_domain_id: string | null
  topology_kind: TopologyKind | null
  allocation: DeviceAllocation | null
  last_seen_at: string | null
}

/** The half of a Pool the plane writes and the cluster obeys. */
export interface PoolDeclaration {
  device_class: string
  node_shape: string
  devices_per_node: number
  min_nodes: number
  max_nodes: number
  provisioning_source: ProvisioningSource
  labels: Record<string, string>
  taints: string[]
}

/** The half the cluster writes and the plane reads. */
export interface PoolMembership {
  nodes_total: number
  nodes_ready: number
  nodes_cordoned: number
  devices_total: number
  devices_healthy: number
  devices_allocated: number
  topology_domains: number
  last_scaled_at: string | null
}

export interface Pool {
  id: string
  name: string
  cluster_id: string
  cluster_name: string
  declared: PoolDeclaration
  observed: PoolMembership
  created_at: string
  updated_at: string | null
}
