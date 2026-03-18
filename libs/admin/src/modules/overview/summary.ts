import type { Cluster } from "@/modules/clusters/types"
import type { Deployment, Endpoint } from "@/modules/demand/types"
import type { Device, SupplyNode } from "@/modules/supply/types"

export interface ClusterSummary {
  total: number
  active: number
  unreachable: number
  outOfSync: number
}

export function summariseClusters(clusters: Cluster[]): ClusterSummary {
  return {
    total: clusters.length,
    active: clusters.filter((c) => c.phase === "active").length,
    unreachable: clusters.filter((c) => !c.reachable).length,
    outOfSync: clusters.filter((c) => c.sync.state !== "connected").length,
  }
}

export interface NodeSummary {
  total: number
  ready: number
  cordoned: number
  notReady: number
}

/**
 * Cordon and readiness are counted separately, never rolled into one "healthy"
 * number: a cordoned node is a drain someone asked for, and a not-ready node is
 * a fault. They need different responses.
 */
export function summariseNodes(nodes: SupplyNode[]): NodeSummary {
  return {
    total: nodes.length,
    ready: nodes.filter((n) => n.ready).length,
    cordoned: nodes.filter((n) => n.cordoned).length,
    notReady: nodes.filter((n) => !n.ready).length,
  }
}

export interface DeviceSummary {
  total: number
  healthy: number
  allocatable: number
  byHealth: { health: string; count: number }[]
  byClass: { deviceClass: string; count: number; healthy: number }[]
  /** Every device that is not plainly healthy, listed rather than counted. */
  impaired: Device[]
}

const HEALTH_ORDER = ["healthy", "degraded", "unhealthy", "unknown"]

function rank(health: string): number {
  const index = HEALTH_ORDER.indexOf(health)
  return index === -1 ? HEALTH_ORDER.length : index
}

/**
 * `impaired` carries the devices themselves, not a count. A single degraded,
 * throttled accelerator disappears into "47 of 48 healthy", and the whole point
 * of the fleet-health panel is that it does not.
 */
export function summariseDevices(devices: Device[]): DeviceSummary {
  const health = new Map<string, number>()
  const classes = new Map<string, { count: number; healthy: number }>()

  for (const device of devices) {
    health.set(device.health, (health.get(device.health) ?? 0) + 1)
    const entry = classes.get(device.device_class) ?? { count: 0, healthy: 0 }
    entry.count += 1
    if (device.health === "healthy") entry.healthy += 1
    classes.set(device.device_class, entry)
  }

  return {
    total: devices.length,
    healthy: health.get("healthy") ?? 0,
    allocatable: devices.filter((d) => d.allocatable).length,
    byHealth: [...health]
      .map(([key, count]) => ({ health: key, count }))
      .sort((a, b) => rank(a.health) - rank(b.health)),
    byClass: [...classes]
      .map(([deviceClass, entry]) => ({ deviceClass, ...entry }))
      .sort((a, b) => b.count - a.count),
    impaired: devices
      .filter((d) => d.health !== "healthy" || d.throttled)
      .sort((a, b) => rank(b.health) - rank(a.health)),
  }
}

export interface ClusterFleetRow {
  cluster: Cluster
  nodes: NodeSummary
  devices: { total: number; healthy: number }
  deployments: number
}

/**
 * Per-cluster rollups derived from the drained node and device lists rather
 * than from a per-cluster fetch, so every row on this page is counted the same
 * way and the totals above it add up.
 */
export function summariseFleet(
  clusters: Cluster[],
  nodes: SupplyNode[],
  devices: Device[],
  deployments: Deployment[]
): ClusterFleetRow[] {
  return clusters.map((cluster) => {
    const clusterDevices = devices.filter((d) => d.cluster === cluster.id)
    return {
      cluster,
      nodes: summariseNodes(nodes.filter((n) => n.cluster === cluster.id)),
      devices: {
        total: clusterDevices.length,
        healthy: clusterDevices.filter((d) => d.health === "healthy").length,
      },
      deployments: deployments.filter((d) => d.cluster_id === cluster.id)
        .length,
    }
  })
}

export interface DemandSummary {
  deployments: number
  deploymentsPending: number
  replicasReady: number
  replicasDesired: number
  endpoints: number
  endpointsEnabled: number
}

export function summariseDemand(
  deployments: Deployment[],
  endpoints: Endpoint[]
): DemandSummary {
  return {
    deployments: deployments.length,
    deploymentsPending: deployments.filter(
      (d) => d.workload.phase === "pending"
    ).length,
    replicasReady: deployments.reduce((sum, d) => sum + d.replicas.ready, 0),
    replicasDesired: deployments.reduce(
      (sum, d) => sum + d.desired_replicas,
      0
    ),
    endpoints: endpoints.length,
    endpointsEnabled: endpoints.filter((e) => e.enabled).length,
  }
}
