/** `FleetClustersSchema`: registered clusters; `reachable` is a live hub session now. */
export interface FleetClusters {
  total: number
  reachable: number
  by_phase: Record<string, number>
}

/** `FleetNodesSchema`: observed nodes, keyed on their reported condition. */
export interface FleetNodes {
  total: number
  ready: number
  cordoned: number
  by_condition: Record<string, number>
}

/** `FleetDeviceClassSchema`: one accelerator class across the fleet. */
export interface FleetDeviceClass {
  device_class: string
  total: number
  free: number
  allocated: number
}

/**
 * `FleetDevicesSchema`: observed accelerators. `free` is claimable and held by
 * no live allocation; `allocated` is `total - free`, the split the cluster
 * rollup makes. A device's `allocatable` flag is not the allocated count.
 */
export interface FleetDevices {
  total: number
  free: number
  allocated: number
  by_health: Record<string, number>
  by_class: FleetDeviceClass[]
}

/**
 * `FleetDeploymentsSchema`: deployments and their replicas.
 * `unschedulable_replicas` is the shortfall of every deployment outside
 * `running` for longer than `stuck_after_seconds`.
 */
export interface FleetDeployments {
  total: number
  by_phase: Record<string, number>
  replicas_ready: number
  clusters_serving: number
  unschedulable_replicas: number
  stuck_after_seconds: number
}

/**
 * `FleetEndpointsSchema`: endpoints by what their own targets say. Disabled
 * endpoints count in `disabled` and nowhere else.
 */
export interface FleetEndpoints {
  total: number
  disabled: number
  serving: number
  degraded: number
  down: number
  idle: number
}

/** `FleetWorkloadsSchema` */
export interface FleetWorkloads {
  total: number
  paused: number
}

/**
 * `FleetSummarySchema`: the fleet in counts from `GET /fleet/summary`, so the
 * overview's tiles need not drain every collection to add them up.
 */
export interface FleetSummary {
  clusters: FleetClusters
  nodes: FleetNodes
  devices: FleetDevices
  deployments: FleetDeployments
  endpoints: FleetEndpoints
  workloads: FleetWorkloads
  generated_at: string
}
