/** `NodeSchema.condition`. Cordon is not one of these — it is its own boolean. */
export type NodeCondition = "ready" | "not_ready" | "unknown" | (string & {})

/** `DeviceSchema.health`. Left open: health vocabulary is the operator's. */
export type DeviceHealth =
  "healthy" | "degraded" | "unhealthy" | "unknown" | (string & {})

export type TopologyKind =
  "nvswitch" | "nvlink" | "pcie" | "numa" | (string & {})

/**
 * `DeviceTopologySchema` — the device's domain inlined for display. A summary
 * and never the authority: it carries no parent, no membership and no counts,
 * so anything that needs the domain itself reads `/topology-domains`.
 */
export interface DeviceTopologyRef {
  id: string
  kind: TopologyKind
  identifier: string
}

/** `TopologyDomainSchema` — observed, and the authority on topology. */
export interface TopologyDomain {
  id: string
  cluster: string
  cluster_name: string
  nodes: string[]
  node_names: string[]
  parent: string | null
  kind: TopologyKind
  identifier: string
  link_generation: string | null
  link_bandwidth_gbps: number | null
  device_count: number
  spans_nodes: boolean
  attributes: Record<string, unknown>
  observed_at: string | null
  created_at: string
  updated_at: string | null
}

/**
 * `NodeDeviceRollupSchema`. State only — how many devices and how they split by
 * health. No utilization, no temperature, no power.
 *
 * `device_class` is the node's dominant GPU model, derived on read, and `null`
 * when the node carries no devices. `allocated` is how many of `total` are
 * committed to a live allocation right now.
 */
export interface NodeDeviceRollup {
  total: number
  by_health: Record<string, number>
  device_class: string | null
  allocated: number
}

/**
 * `NodeConditionSchema` — one observed node condition as the cluster reported
 * it. Generic health signals (memory/disk pressure, network availability) kept
 * in full; the plane does not interpret `type`.
 */
export interface NodeConditionDetail {
  type: string
  status: string
  reason: string
  message: string
  since: string
}

/** `NodesSummarySchema` — fleet-wide node rollup, computed server-side. */
export interface NodesSummary {
  total: number
  ready: number
  cordoned: number
  faulted_devices: number
}

/** `NodeSchema` — a machine observed inside a cluster. Nothing here is declared. */
export interface SupplyNode {
  id: string
  name: string
  cluster: string
  cluster_name: string
  pool: string | null
  pool_name: string | null
  condition: NodeCondition
  /**
   * Per-signal observed conditions as the cluster named them — memory or disk
   * pressure, network availability. The plane does not interpret `type`.
   */
  conditions: NodeConditionDetail[]
  /**
   * Observed schedulability. Its own boolean rather than a value of
   * `condition`: cordon is the state a drain Operation leaves behind, and
   * "cordoned" and "not ready" are different facts needing different responses.
   */
  cordoned: boolean
  ready: boolean
  /**
   * What the machine is, as its provider names it — `p5.48xlarge`. The answer to
   * "what kind of node is this", which a node count never gives.
   */
  instance_type: string | null
  cloud: string | null
  zone: string | null
  region: string | null
  cpu_cores: number | null
  memory_mib: number | null
  cpu_arch: string | null
  os_image: string | null
  kernel_version: string | null
  driver_version: string | null
  operator_version: string | null
  labels: Record<string, string>
  taints: unknown[]
  devices: NodeDeviceRollup
  joined_at: string | null
  observed_at: string | null
  created_at: string
  updated_at: string | null
}

/**
 * `DeviceSchema` — one accelerator, the schedulable unit, always observed.
 *
 * There is no allocation object here and no telemetry: what holds a device is
 * answered by `/allocations?device=`, and utilization, temperature and power go
 * to the observability stack directly.
 */
export interface Device {
  id: string
  cluster: string
  cluster_name: string
  node: string
  node_name: string
  device_uuid: string
  device_index: number
  device_class: string
  vendor: string
  model_name: string | null
  pci_address: string | null
  memory_mib: number
  compute_capability: string | null
  health: DeviceHealth
  health_reason: string | null
  ecc_enabled: boolean
  ecc_volatile_errors: number
  ecc_aggregate_errors: number
  ecc_pending_retirement: boolean
  throttled: boolean
  throttle_reasons: string[]
  mig_enabled: boolean
  mig_profile: string | null
  mig_parent_device_uuid: string | null
  allocatable: boolean
  topology: DeviceTopologyRef | null
  observed_at: string | null
  created_at: string
  updated_at: string | null
}

/**
 * `DeviceClassInventorySchema` — what the fleet holds of one class, counted on read.
 *
 * `allocatable` is free — claimable and unclaimed — and `allocated` is held by
 * a live allocation. Neither is the complement of the other: a device that is
 * unschedulable and unheld is in neither count, and a held device can be
 * unhealthy. Never derive one from the other.
 */
export interface DeviceClassInventory {
  devices: number
  healthy: number
  allocatable: number
  allocated: number
  nodes: number
  clusters: string[]
}

/**
 * `DeviceClassSchema` — one GPU model, as the plane names it.
 *
 * `name` is the identifier: the string that appears on a pool, a recipe shape,
 * a quota and a device, and the only one that goes back over the wire. `label`
 * is the only one to put in front of a person.
 *
 * `in_catalog` false means the fleet is running hardware the contract table
 * does not name — it resolved to the slug of its vendor string and is being
 * scheduled onto regardless. Those rows are the ones worth acting on.
 */
export interface DeviceClass {
  name: string
  label: string
  architecture: string
  vendor: string
  memory_mib: number
  form_factor: string
  in_catalog: boolean
  aliases: Record<string, string[]>
  inventory: DeviceClassInventory
}

/** `PoolMembershipSchema` — the observed half of a pool, as a rollup. */
export interface PoolMembership {
  nodes_total: number
  nodes_ready: number
  nodes_cordoned: number
  devices_total: number
  devices_healthy: number
  devices_allocatable: number
}

/** `PoolSchema` — declared spec and observed membership, side by side. */
export interface Pool {
  id: string
  cluster_id: string
  name: string
  device_class: string
  devices_per_node: number
  node_shape: Record<string, unknown>
  min_nodes: number
  max_nodes: number
  labels: Record<string, string>
  taints: unknown[]
  enabled: boolean
  created_at: string
  updated_at: string | null
  revision: string
  membership: PoolMembership
  /** Derived by the plane so the console and `hgr` cannot disagree about it. */
  within_declared_bounds: boolean
}

export interface PoolCreateRequest {
  /** The plane's field is `cluster` and takes an id or a name, not `cluster_id`. */
  cluster: string
  name: string
  device_class: string
  devices_per_node?: number
  node_shape?: Record<string, unknown>
  min_nodes?: number
  max_nodes?: number
  labels?: Record<string, string>
  taints?: unknown[]
  enabled?: boolean
}

export interface PoolUpdateRequest {
  device_class?: string
  devices_per_node?: number
  node_shape?: Record<string, unknown>
  min_nodes?: number
  max_nodes?: number
  labels?: Record<string, string>
  taints?: unknown[]
  enabled?: boolean
}
