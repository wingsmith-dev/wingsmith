import type { ResourceKind } from "@/components/layout/resource-icon"
import type { StatusTone } from "@/components/layout/status-dot"
import { formatRelative } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { clusterStage } from "@/modules/clusters/stage"
import type { Cluster } from "@/modules/clusters/types"
import type { Deployment, Endpoint } from "@/modules/demand/types"
import type { DiscoveredWorkload } from "@/modules/discovered/types"
import { summarizeDevices } from "@/modules/overview/summary"
import type { Device, SupplyNode } from "@/modules/supply/types"
import type { LinkProps } from "@tanstack/react-router"
import { stuckDeployments, summarizeEndpointHealth } from "./fleet"

/**
 * Which half of the inbox a row belongs to. `discovered` is a resource running
 * outside the catalog and `health` is something serving badly. They share a
 * screen because both are "somebody has to decide", but the tabs keep them
 * apart because the decisions are unrelated.
 */
export type AttentionGroup = "health" | "discovered"

export interface AttentionItem {
  id: string
  group: AttentionGroup
  tone: StatusTone
  kind: ResourceKind
  title: string
  detail: string
  /** When the condition started, rendered as an age. Absent where nothing dates it. */
  since?: string | null
  /** What kind of thing was sighted. Discovered rows only. */
  pill?: string
  action: { label: string; link: LinkProps }
}

export interface FleetAttentionInput {
  clusters?: Cluster[]
  nodes?: SupplyNode[]
  devices?: Device[]
  deployments?: Deployment[]
  endpoints?: Endpoint[]
  discovered?: DiscoveredWorkload[]
}

const TONE_RANK: Record<StatusTone, number> = {
  danger: 0,
  warn: 1,
  info: 2,
  ok: 3,
  neutral: 4,
}

function endpointItems(
  endpoints: Endpoint[],
  deployments: Deployment[]
): AttentionItem[] {
  return summarizeEndpointHealth(endpoints, deployments)
    .rows.filter((row) => row.health === "down" || row.health === "degraded")
    .map((row) => ({
      id: `endpoint:${row.endpoint.id}`,
      group: "health" as const,
      tone: row.health === "down" ? ("danger" as const) : ("warn" as const),
      kind: "endpoint" as const,
      title:
        row.health === "down"
          ? `${row.endpoint.name} has no healthy replicas`
          : `${row.endpoint.name} is serving on ${row.ready} of ${row.desired} replicas`,
      detail: `endpoint · ${row.targets} ${row.targets === 1 ? "target" : "targets"} · ${row.endpoint.routing_strategy}`,
      action: {
        label: "Investigate",
        link: {
          to: "/endpoints/$endpointId",
          params: { endpointId: row.endpoint.id },
        },
      },
    }))
}

/**
 * A running deployment short of replicas. Deployments still being placed are
 * left to {@link placementItem}: the shortfall and the phase are the same fact
 * told twice while placement is in flight.
 */
function deploymentItems(deployments: Deployment[]): AttentionItem[] {
  return deployments
    .filter(
      (deployment) =>
        deployment.admission_phase === "running" &&
        deployment.enabled &&
        deployment.desired_replicas > 0 &&
        deployment.replicas.ready < deployment.desired_replicas
    )
    .map((deployment) => ({
      id: `deployment:${deployment.id}`,
      group: "health" as const,
      tone:
        deployment.replicas.ready === 0
          ? ("danger" as const)
          : ("warn" as const),
      kind: "deployment" as const,
      title: `${deployment.name} degraded — ${
        deployment.desired_replicas - deployment.replicas.ready
      } of ${deployment.desired_replicas} replicas down`,
      detail: `workload ${deployment.workload_name} · ${
        deployment.status?.reason ??
        `${deployment.replicas.failed} failed · ${deployment.replicas.pending} pending`
      }`,
      since: deployment.status?.observed_at ?? null,
      action: {
        label: "Investigate",
        link: {
          to: "/deployments/$deploymentId",
          params: { deploymentId: deployment.id },
        },
      },
    }))
}

/**
 * Everything the fleet cannot place, as one row. Unschedulable replicas are a
 * single condition with one cause — there is no capacity — and one row per
 * deployment would bury the health rows under it.
 */
function placementItem(deployments: Deployment[]): AttentionItem[] {
  const stuck = stuckDeployments(deployments)
  if (stuck.length === 0) return []

  const replicas = stuck.reduce((sum, entry) => sum + entry.shortfall, 0)
  const reason = stuck.find((entry) => entry.deployment.admission_reason)
    ?.deployment.admission_reason

  return [
    {
      id: "placement:unschedulable",
      group: "health",
      tone: "warn",
      kind: "allocation",
      title: `${replicas} ${replicas === 1 ? "replica" : "replicas"} unschedulable`,
      detail: `placement · ${stuck.length} ${
        stuck.length === 1 ? "deployment" : "deployments"
      } affected${reason ? ` · ${reason}` : ""}`,
      since: stuck[0].since,
      action: { label: "View", link: { to: "/deployments" } },
    },
  ]
}

function clusterItems(clusters: Cluster[]): AttentionItem[] {
  const out: AttentionItem[] = []

  for (const cluster of clusters) {
    const stage = clusterStage(cluster)
    const link: LinkProps = {
      to: "/clusters/$clusterId",
      params: { clusterId: cluster.id },
    }

    if (stage === "onboarding") {
      out.push({
        id: `cluster:not-enrolled:${cluster.id}`,
        group: "health",
        tone: "info",
        kind: "cluster",
        title: `Cluster ${clusterLabel(cluster)} is not enrolled`,
        detail: "install the cluster-setup chart to connect its operator",
        action: { label: "View", link },
      })
      continue
    }

    if (stage !== "degraded") continue

    out.push({
      id: `cluster:degraded:${cluster.id}`,
      group: "health",
      tone: cluster.reachable ? "warn" : "danger",
      kind: "cluster",
      title: `Cluster ${clusterLabel(cluster)} last reconciled ${formatRelative(
        cluster.sync.last_sync_at
      )}`,
      detail:
        cluster.sync.last_error ??
        "agent unreachable · counts below may be stale",
      since: cluster.sync.last_sync_at,
      action: { label: "View", link },
    })
  }

  return out
}

function deviceItems(devices: Device[]): AttentionItem[] {
  return summarizeDevices(devices).impaired.map((device) => ({
    id: `device:${device.id}`,
    group: "health" as const,
    tone:
      device.health === "unhealthy" ? ("danger" as const) : ("warn" as const),
    kind: "device" as const,
    title: `${device.node_name} · gpu ${device.device_index} is ${device.health}${
      device.throttled ? " and throttled" : ""
    }`,
    detail: [device.cluster_name, device.device_class, device.health_reason]
      .filter(Boolean)
      .join(" · "),
    since: device.observed_at,
    action: {
      label: "View",
      link: { to: "/devices/$deviceId", params: { deviceId: device.id } },
    },
  }))
}

function nodeItems(nodes: SupplyNode[]): AttentionItem[] {
  return nodes
    .filter((node) => !node.ready)
    .map((node) => ({
      id: `node:${node.id}`,
      group: "health" as const,
      tone: "danger" as const,
      kind: "node" as const,
      title: `${node.name} is ${node.condition.replace(/_/g, " ")}`,
      detail: [
        node.cluster_name,
        node.pool_name,
        `${node.devices.total} devices`,
      ]
        .filter(Boolean)
        .join(" · "),
      since: node.observed_at,
      action: {
        label: "View",
        link: { to: "/nodes/$nodeId", params: { nodeId: node.id } },
      },
    }))
}

/**
 * Workloads a cluster was already running when it was connected and nobody has ruled
 * on. Neutral rather than warn: it is a decision waiting, not a fault.
 */
function discoveredItems(discovered: DiscoveredWorkload[]): AttentionItem[] {
  return discovered.map((sighting) => ({
    id: `discovered:${sighting.id}`,
    group: "discovered" as const,
    tone: "neutral" as const,
    kind: "discovered" as const,
    title: `${sighting.name} running outside Hangar`,
    detail: `${sighting.cluster_name} · ns/${sighting.namespace} · ${
      sighting.replicas_ready
    }/${sighting.replicas_desired} replicas · ${sighting.devices_total} devices`,
    pill: sighting.kind,
    since: sighting.first_seen_at,
    action: { label: "Adopt", link: { to: "/discovered" } },
  }))
}

/**
 * Everything on the fleet a human should decide about, and nothing else.
 *
 * The fleet counterpart of `deriveClusterActionables`, and deliberately not the
 * same list: this one is ordered by how loud a row is rather than grouped by
 * cluster, and it carries the discovered half that a cluster page shows in its
 * own panel. Each source is optional so one failed query costs its own rows
 * rather than the whole inbox.
 */
export function deriveFleetAttention({
  clusters,
  nodes,
  devices,
  deployments,
  endpoints,
  discovered,
}: FleetAttentionInput): AttentionItem[] {
  return [
    ...endpointItems(endpoints ?? [], deployments ?? []),
    ...deploymentItems(deployments ?? []),
    ...placementItem(deployments ?? []),
    ...clusterItems(clusters ?? []),
    ...deviceItems(devices ?? []),
    ...nodeItems(nodes ?? []),
    ...discoveredItems(discovered ?? []),
  ].sort((a, b) => TONE_RANK[a.tone] - TONE_RANK[b.tone])
}
