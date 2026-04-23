import type { StatusTone } from "@/components/layout/status-dot"
import { formatRelative } from "@/lib/format"
import { CLUSTER_STAGE_META, clusterStage } from "@/modules/clusters/stage"
import type { ClusterStage } from "@/modules/clusters/stage"
import type { Cluster } from "@/modules/clusters/types"
import type { Deployment, Endpoint } from "@/modules/demand/types"
import type { Device } from "@/modules/supply/types"

/**
 * How long a deployment may sit outside `running` before it is worth chasing.
 * The same ten minutes `deriveClusterActionables` gives a rollout, said again
 * here because the fleet page must not call something stuck that a cluster page
 * calls settling.
 */
export const STUCK_MS = 10 * 60 * 1000

/**
 * What an endpoint's own targets say about it, and nothing the gateway measured.
 *
 * `down` is "no target has a ready replica", which is the only serving fact the
 * control plane can state: request rates and error rates never cross it.
 * `idle` is an endpoint with nothing to point at — declared ahead of its
 * deployments, not broken.
 */
export type EndpointHealth = "serving" | "degraded" | "down" | "idle"

export interface EndpointHealthRow {
  endpoint: Endpoint
  health: EndpointHealth
  ready: number
  desired: number
  targets: number
}

export interface EndpointHealthSummary {
  serving: number
  degraded: number
  down: number
  idle: number
  rows: EndpointHealthRow[]
}

/**
 * Endpoint health folded out of the deployments its targets name. Disabled
 * endpoints are left out entirely: an endpoint nobody serves from is not a
 * degraded one.
 */
export function summariseEndpointHealth(
  endpoints: Endpoint[],
  deployments: Deployment[]
): EndpointHealthSummary {
  const byId = new Map(deployments.map((d) => [d.id, d]))
  const rows: EndpointHealthRow[] = []

  for (const endpoint of endpoints) {
    if (!endpoint.enabled) continue
    const targets = endpoint.targets
      .filter((target) => target.enabled)
      .map((target) => byId.get(target.deployment_id))
      .filter(
        (deployment): deployment is Deployment => deployment !== undefined
      )

    const ready = targets.reduce((sum, d) => sum + d.replicas.ready, 0)
    const desired = targets.reduce((sum, d) => sum + d.desired_replicas, 0)
    const health: EndpointHealth =
      targets.length === 0
        ? "idle"
        : ready === 0
          ? "down"
          : ready < desired
            ? "degraded"
            : "serving"

    rows.push({ endpoint, health, ready, desired, targets: targets.length })
  }

  return {
    serving: rows.filter((row) => row.health === "serving").length,
    degraded: rows.filter((row) => row.health === "degraded").length,
    down: rows.filter((row) => row.health === "down").length,
    idle: rows.filter((row) => row.health === "idle").length,
    rows,
  }
}

/** One deployment that has been mid-flight long enough to look wedged. */
export interface StuckDeployment {
  deployment: Deployment
  /** Replicas the cluster owes this deployment and has not produced. */
  shortfall: number
  since: string | null
}

function phaseStamp(deployment: Deployment): string | null {
  return deployment.placed_at ?? deployment.admitted_at ?? deployment.queued_at
}

/**
 * Deployments outside `running` for longer than {@link STUCK_MS}. Placement
 * takes time, so a rollout two seconds old is working rather than unschedulable.
 */
export function stuckDeployments(
  deployments: Deployment[],
  now: number = Date.now()
): StuckDeployment[] {
  const out: StuckDeployment[] = []

  for (const deployment of deployments) {
    if (deployment.admission_phase === "running") continue
    const since = phaseStamp(deployment)
    const elapsed = since === null ? null : now - new Date(since).getTime()
    if (elapsed !== null && elapsed <= STUCK_MS) continue
    out.push({
      deployment,
      shortfall: Math.max(
        deployment.desired_replicas - deployment.replicas.ready,
        0
      ),
      since,
    })
  }

  return out.sort((a, b) => (a.since ?? "").localeCompare(b.since ?? ""))
}

/** How many replicas the fleet has asked for and cannot place. */
export function unschedulableReplicas(
  deployments: Deployment[],
  now?: number
): number {
  return stuckDeployments(deployments, now).reduce(
    (sum, entry) => sum + entry.shortfall,
    0
  )
}

export interface CapacityRow {
  deviceClass: string
  total: number
  allocated: number
  /** Whole percent, so the bar and the label cannot disagree. */
  percent: number
}

/**
 * Allocation pressure per accelerator, which is the number that predicts a
 * failed placement. Allocated is `total - allocatable`, derived the same way the
 * plane's own cluster rollup derives it.
 */
export function capacityByClass(devices: Device[]): CapacityRow[] {
  const classes = new Map<string, { total: number; allocated: number }>()

  for (const device of devices) {
    const entry = classes.get(device.device_class) ?? { total: 0, allocated: 0 }
    entry.total += 1
    if (!device.allocatable) entry.allocated += 1
    classes.set(device.device_class, entry)
  }

  return [...classes]
    .map(([deviceClass, entry]) => ({
      deviceClass,
      ...entry,
      percent:
        entry.total === 0
          ? 0
          : Math.round((entry.allocated / entry.total) * 100),
    }))
    .sort((a, b) => b.percent - a.percent || b.total - a.total)
}

/** Fleet-wide device allocation, the headline the per-class rows break down. */
export function allocationTotals(devices: Device[]): {
  total: number
  allocated: number
  percent: number
} {
  const allocated = devices.filter((device) => !device.allocatable).length
  return {
    total: devices.length,
    allocated,
    percent:
      devices.length === 0 ? 0 : Math.round((allocated / devices.length) * 100),
  }
}

/**
 * One row of the reachability panel: what the cluster's link is doing, said as
 * the derived stage rather than as phase or sync state alone.
 */
export interface ReachabilityRow {
  cluster: Cluster
  stage: ClusterStage
  tone: StatusTone
  /** The age of the last reconcile, or what the cluster is doing instead. */
  note: string
  /** True when the observed state under this cluster is not fresh. */
  stale: boolean
}

export function reachabilityRows(clusters: Cluster[]): ReachabilityRow[] {
  return clusters.map((cluster) => {
    const stage = clusterStage(cluster)
    return {
      cluster,
      stage,
      tone: CLUSTER_STAGE_META[stage].tone,
      note:
        stage === "onboarding"
          ? "not enrolled"
          : formatRelative(
              cluster.sync.last_sync_at ?? cluster.sync.last_seen_at
            ),
      stale: stage === "degraded",
    }
  })
}

/**
 * The clusters whose observed state is stale, so that every count on the page
 * can be caveated by name. A page that silently under-counts is worse than one
 * that says which cluster it stopped hearing from.
 */
export function staleClusters(clusters: Cluster[]): Cluster[] {
  return reachabilityRows(clusters)
    .filter((row) => row.stale)
    .map((row) => row.cluster)
}

/** Replicas ready fleet-wide, and how many clusters are carrying them. */
export function replicaFootprint(deployments: Deployment[]): {
  ready: number
  clusters: number
} {
  return {
    ready: deployments.reduce((sum, d) => sum + d.replicas.ready, 0),
    clusters: new Set(
      deployments.filter((d) => d.replicas.ready > 0).map((d) => d.cluster_id)
    ).size,
  }
}
