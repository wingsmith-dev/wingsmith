import type { StatusTone } from "@/components/layout/status-dot"
import { formatRelative } from "@/lib/format"
import type { ResourceKind } from "@/lib/resource-kinds"
import type { Cluster } from "@/modules/clusters/types"
import type { Deployment } from "@/modules/demand/types"
import type { Operation } from "@/modules/machinery/types"
import type { Device, Pool, SupplyNode } from "@/modules/supply/types"
import type { LinkProps } from "@tanstack/react-router"

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

export interface Actionable {
  id: string
  tone: StatusTone
  kind: ResourceKind
  title: string
  detail?: string
  badges?: string[]
  link: LinkProps
}

export interface ClusterActionableInput {
  cluster: Cluster
  pools?: Pool[]
  nodes?: SupplyNode[]
  devices?: Device[]
  deployments?: Deployment[]
  operations?: Operation[]
}

const STALE_SYNC_MS = 5 * 60 * 1000

/**
 * How long a deployment may sit in a non-running phase before it is worth
 * chasing. Placement takes time, and a rollout that is two seconds into
 * `admitted` is working, not stuck.
 */
const SETTLING_MS = 10 * 60 * 1000

const TONE_RANK: Record<StatusTone, number> = {
  danger: 0,
  warn: 1,
  info: 2,
  ok: 3,
  neutral: 4,
}

/** A deployment phase nobody has to chase. Everything else is still moving. */
const SETTLED_PHASES = ["running"]

/**
 * One fault, one row. An unreachable cluster also has a broken sync link and a
 * stale last sync, and saying so three times does not make it three problems —
 * the downstream rows only appear where the cluster is otherwise reachable.
 */
function syncActionables(cluster: Cluster): Actionable[] {
  const out: Actionable[] = []
  const link: LinkProps = {
    to: "/clusters/$clusterId",
    params: { clusterId: cluster.id },
  }

  // Asked before reachability, which is false for a cluster that never connected
  // — the plane has no sync row to say otherwise. Reading that as unreachable
  // files a permanent red fault against every cluster declared ahead of its
  // install, and reports a link as broken that was never established.
  if (cluster.sync.state === "never_connected") {
    return [
      {
        id: "cluster:not-enrolled",
        tone: "info",
        kind: "cluster",
        title: "Cluster is not enrolled",
        detail: "install the cluster-setup chart to connect its operator",
        link,
      },
    ]
  }

  if (!cluster.reachable) {
    return [
      {
        id: "cluster:unreachable",
        tone: "danger",
        kind: "cluster",
        title: "Cluster is unreachable",
        detail: `last seen ${formatRelative(cluster.sync.last_seen_at)}`,
        link,
      },
    ]
  }

  if (cluster.sync.state !== "connected") {
    out.push({
      id: "cluster:sync",
      tone: "danger",
      kind: "cluster",
      title: `Sync link is ${cluster.sync.state.replace(/_/g, " ")}`,
      detail: cluster.sync.last_error ?? undefined,
      badges: cluster.sync.operator_version
        ? [`operator ${cluster.sync.operator_version}`]
        : undefined,
      link,
    })
  }

  const lastSync = cluster.sync.last_sync_at
  if (
    out.length === 0 &&
    lastSync &&
    Date.now() - new Date(lastSync).getTime() > STALE_SYNC_MS
  ) {
    out.push({
      id: "cluster:stale",
      tone: "warn",
      kind: "cluster",
      title: `Last sync was ${formatRelative(lastSync)}`,
      detail: "observed state on this page is at least that old",
      link,
    })
  }

  return out
}

/**
 * Bounds are stated as the two numbers themselves, not as a ratio: "wants 4
 * nodes, has 3" is a thing to go and fix, and "75% of declared" is not.
 */
function poolActionables(pools: Pool[]): Actionable[] {
  const out: Actionable[] = []

  for (const pool of pools) {
    const have = pool.membership.nodes_total
    const link: LinkProps = {
      to: "/pools/$poolId",
      params: { poolId: pool.id },
    }
    const badges = [pool.device_class, ...(pool.enabled ? [] : ["disabled"])]

    if (have < pool.min_nodes) {
      out.push({
        id: `pool:under:${pool.id}`,
        tone: "danger",
        kind: "pool",
        title: `${pool.name} wants ${pool.min_nodes} nodes, has ${have}`,
        detail: `${pool.min_nodes - have} short of the declared minimum`,
        badges,
        link,
      })
    } else if (have > pool.max_nodes) {
      out.push({
        id: `pool:over:${pool.id}`,
        tone: "warn",
        kind: "pool",
        title: `${pool.name} has ${have} nodes, max is ${pool.max_nodes}`,
        badges,
        link,
      })
    } else if (!pool.within_declared_bounds) {
      out.push({
        id: `pool:bounds:${pool.id}`,
        tone: "warn",
        kind: "pool",
        title: `${pool.name} is outside its declared bounds`,
        detail: `${have} nodes against ${pool.min_nodes}–${pool.max_nodes}`,
        badges,
        link,
      })
    }
  }

  return out
}

function deviceActionables(devices: Device[]): Actionable[] {
  return summariseDevices(devices).impaired.map((device) => ({
    id: `device:${device.id}`,
    tone:
      device.health === "unhealthy" ? ("danger" as const) : ("warn" as const),
    kind: "device" as const,
    title: `${device.node_name} · gpu ${device.device_index} is ${device.health}${
      device.throttled ? " and throttled" : ""
    }`,
    detail: device.health_reason ?? undefined,
    badges: [device.device_class, ...device.throttle_reasons],
    link: { to: "/devices/$deviceId", params: { deviceId: device.id } },
  }))
}

function nodeActionables(nodes: SupplyNode[]): Actionable[] {
  const out: Actionable[] = []

  for (const node of nodes) {
    const link: LinkProps = {
      to: "/nodes/$nodeId",
      params: { nodeId: node.id },
    }

    if (!node.ready) {
      out.push({
        id: `node:notready:${node.id}`,
        tone: "danger",
        kind: "node",
        title: `${node.name} is ${node.condition.replace(/_/g, " ")}`,
        detail: `last observed ${formatRelative(node.observed_at)}`,
        badges: node.pool_name ? [node.pool_name] : undefined,
        link,
      })
    }

    if (node.cordoned) {
      out.push({
        id: `node:cordoned:${node.id}`,
        tone: "info",
        kind: "node",
        title: `${node.name} is cordoned`,
        detail: "nothing new will be scheduled onto it until it is uncordoned",
        badges: node.pool_name ? [node.pool_name] : undefined,
        link,
      })
    }
  }

  return out
}

/**
 * How long a deployment has been in the phase it is in, where we can tell.
 *
 * Admission is per-cluster, so the three stamps are the deployment's own: a
 * workload queued on quota in one region says nothing about another.
 */
function settlingFor(deployment: Deployment): number | null {
  const stamp =
    deployment.placed_at ?? deployment.admitted_at ?? deployment.queued_at
  return stamp ? Date.now() - new Date(stamp).getTime() : null
}

/**
 * A rollout in flight is not a thing to do. Only a deployment that has been
 * mid-flight long enough to look stuck earns a row, and it earns one row: the
 * phase and the replica shortfall are the same fact told twice while placement
 * is still running.
 */
function deploymentActionables(deployments: Deployment[]): Actionable[] {
  const out: Actionable[] = []

  for (const deployment of deployments) {
    const link: LinkProps = {
      to: "/deployments/$deploymentId",
      params: { deploymentId: deployment.id },
    }

    const settling = !SETTLED_PHASES.includes(deployment.admission_phase)
    const elapsed = settlingFor(deployment)
    const stuck = settling && (elapsed === null || elapsed > SETTLING_MS)

    if (stuck) {
      out.push({
        id: `deployment:phase:${deployment.id}`,
        tone: deployment.admission_phase === "pending" ? "warn" : "info",
        kind: "deployment",
        title: `${deployment.name} has been ${deployment.admission_phase}${
          elapsed === null
            ? ""
            : ` since ${formatRelative(
                deployment.placed_at ??
                  deployment.admitted_at ??
                  deployment.queued_at
              )}`
        }`,
        detail: deployment.admission_reason ?? undefined,
        badges: [deployment.workload_name],
        link,
      })
    }

    if (
      !settling &&
      deployment.enabled &&
      deployment.desired_replicas > 0 &&
      deployment.replicas.ready < deployment.desired_replicas
    ) {
      out.push({
        id: `deployment:replicas:${deployment.id}`,
        tone: deployment.replicas.ready === 0 ? "danger" : "warn",
        kind: "deployment",
        title: `${deployment.name} has ${deployment.replicas.ready} of ${deployment.desired_replicas} replicas ready`,
        detail:
          deployment.replicas.failed > 0
            ? `${deployment.replicas.failed} failed · ${deployment.replicas.pending} pending`
            : `${deployment.replicas.pending} pending`,
        link,
      })
    }
  }

  return out
}

function operationActionables(operations: Operation[]): Actionable[] {
  return operations.map((operation) => ({
    id: `operation:${operation.id}`,
    tone: "info" as const,
    kind: "operation" as const,
    title: `${operation.type} is ${operation.phase}`,
    detail: `${operation.target.type} ${operation.target.id} · requested ${formatRelative(
      operation.created_at
    )}`,
    badges:
      operation.attempts > 1 ? [`attempt ${operation.attempts}`] : undefined,
    link: {
      to: "/operations/$operationId",
      params: { operationId: operation.id },
    },
  }))
}

/**
 * Everything on this cluster a human should do something about, and nothing
 * else. Each source is optional so one failed sub-query costs its own rows
 * rather than the whole list, and the order is by how loud the thing is.
 */
export function deriveClusterActionables({
  cluster,
  pools,
  nodes,
  devices,
  deployments,
  operations,
}: ClusterActionableInput): Actionable[] {
  return [
    ...syncActionables(cluster),
    ...poolActionables(pools ?? []),
    ...deviceActionables(devices ?? []),
    ...nodeActionables(nodes ?? []),
    ...deploymentActionables(deployments ?? []),
    ...operationActionables(operations ?? []),
  ].sort((a, b) => TONE_RANK[a.tone] - TONE_RANK[b.tone])
}
