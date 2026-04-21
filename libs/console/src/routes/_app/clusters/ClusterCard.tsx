import { RowActions } from "@/components/layout/destructive"
import { StatusDot, type StatusTone } from "@/components/layout/status-dot"
import { Card } from "@/components/ui/card"
import { formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"
import { clusterLabel } from "@/modules/clusters/display"
import type { Cluster, ClusterRollup } from "@/modules/clusters/types"
import { Link } from "@tanstack/react-router"
import { IconActivity, IconAlertTriangle } from "@tabler/icons-react"

type ClusterHealth = "healthy" | "degraded" | "down" | "provisioning"

const HEALTH_TONE: Record<ClusterHealth, StatusTone> = {
  healthy: "ok",
  degraded: "warn",
  down: "danger",
  provisioning: "neutral",
}

const HEALTH_RING: Record<ClusterHealth, string> = {
  healthy: "ring-foreground/10",
  degraded: "ring-amber-500/30",
  down: "ring-destructive/35",
  provisioning: "ring-foreground/10",
}

/**
 * Collapses the declared phase and observed reachability/sync into a single
 * at-a-glance health, only for the card's dot and border. Phase and
 * reachability are still kept apart everywhere they carry a decision — a
 * disabled cluster is somebody's choice, an unreachable one is a network fault.
 */
function clusterHealth(cluster: Cluster): ClusterHealth {
  if (!cluster.reachable || cluster.sync.state === "disconnected") return "down"
  if (cluster.phase === "pending") return "provisioning"
  if (cluster.phase === "draining" || cluster.sync.state === "stale")
    return "degraded"
  return "healthy"
}

/**
 * Phase and reachability sit next to each other and are never folded into one
 * badge. A cluster that is `active` and unreachable is a network problem; a
 * `disabled` one is somebody's decision, and an operator has to be able to tell
 * them apart at a glance.
 */
export function ClusterCard({
  cluster,
  rollup,
  onEdit,
  onDelete,
}: {
  cluster: Cluster
  rollup: ClusterRollup | undefined
  onEdit: () => void
  onDelete: () => void
}) {
  const health = clusterHealth(cluster)
  const tone = HEALTH_TONE[health]

  const region = [cluster.region, cluster.sync.cloud, cluster.sync.distribution]
    .filter(Boolean)
    .join(" · ")

  const gpuTotal = rollup?.devices_total ?? 0
  const gpuAllocated = rollup ? rollup.devices_allocated : null
  const gpuPct =
    gpuAllocated != null && gpuTotal > 0
      ? Math.round((gpuAllocated / gpuTotal) * 100)
      : 0
  const fillTone =
    gpuPct >= 90 ? "bg-amber-500" : gpuPct <= 30 ? "bg-muted-foreground/70" : "bg-primary"

  const nodeTotal = rollup?.nodes_total ?? 0
  const nodeReady = rollup ? rollup.nodes_ready : null
  const deployments = rollup ? rollup.deployment_count : null
  const pools = rollup ? rollup.pool_count : null

  const noteTone = health === "down" ? "down" : "warn"
  const note =
    cluster.sync.last_error ??
    (health === "provisioning"
      ? "Provisioning — nodes still joining. Not schedulable yet."
      : health === "down"
        ? "Agent unreachable. Last known state shown; replicas may still be serving."
        : null)
  const showNote = health === "down" || health === "degraded" || health === "provisioning"

  return (
    <Card size="sm" className={cn("gap-3 p-0", HEALTH_RING[health])}>
      <div className="flex flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Link
              to="/clusters/$clusterId"
              params={{ clusterId: cluster.id }}
              className="flex items-center gap-2 truncate font-medium tracking-tight hover:text-primary"
            >
              <StatusDot tone={tone} pulse={cluster.sync.state === "connected"} />
              {clusterLabel(cluster)}
            </Link>
            <span className="mt-1 block truncate font-mono text-2xs text-muted-foreground/60">
              {region}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <span
              className={cn(
                "inline-flex items-center gap-1 font-mono text-2xs text-muted-foreground",
                !cluster.reachable && "text-amber-500"
              )}
            >
              <IconActivity className="size-3" />
              {formatRelative(cluster.sync.last_seen_at ?? cluster.sync.last_sync_at)}
            </span>
            <RowActions
              actions={[
                { label: "Edit", onSelect: onEdit },
                { label: "Delete", onSelect: onDelete, destructive: true },
              ]}
            />
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-baseline justify-between">
            <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
              GPUs allocated
            </span>
            <span className="font-mono text-xs tabular-nums">
              {gpuAllocated ?? "?"}
              <span className="text-muted-foreground/60">/{gpuTotal}</span>
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-secondary">
            <div
              className={cn("h-full rounded-full", fillTone)}
              style={{ width: `${gpuPct}%` }}
            />
          </div>
        </div>

        {showNote && note && (
          <div
            className={cn(
              "flex items-center gap-1.5 rounded-md px-2 py-1.5 text-2xs leading-snug",
              noteTone === "down"
                ? "bg-destructive/10 text-destructive"
                : "bg-amber-500/10 text-amber-500"
            )}
          >
            <IconAlertTriangle className="size-3.5 shrink-0" />
            <span>{note}</span>
          </div>
        )}

        <div className="grid grid-cols-3 gap-2 border-t pt-3">
          <Stat
            label="Nodes"
            value={nodeReady != null ? `${nodeReady}/${nodeTotal}` : "—"}
            warn={nodeReady != null && nodeReady < nodeTotal}
            muted={nodeReady == null}
          />
          <Stat
            label="Deployments"
            value={deployments != null ? String(deployments) : "—"}
            muted={deployments == null}
          />
          <Stat
            label="Pools"
            value={pools != null ? String(pools) : "—"}
            muted={pools == null}
          />
        </div>
      </div>
    </Card>
  )
}

function Stat({
  label,
  value,
  warn,
  muted,
}: {
  label: string
  value: string
  warn?: boolean
  muted?: boolean
}) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </span>
      <span
        className={cn(
          "font-mono text-xs tabular-nums",
          warn && "text-amber-500",
          muted && "text-muted-foreground/50"
        )}
      >
        {value}
      </span>
    </div>
  )
}
