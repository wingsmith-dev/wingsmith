import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import {
  DEVICE_HEALTH_TONE,
  NODE_CONDITION_LABEL,
  NODE_CONDITION_TONE,
  TOPOLOGY_KIND_LABEL,
} from "@/modules/supply/display"
import type {
  Device,
  DeviceAllocation,
  NodeCondition,
  TopologyKind,
} from "@/modules/supply/types"
import { Link } from "@tanstack/react-router"

export function NodeConditionLabel({ condition }: { condition: NodeCondition }) {
  return (
    <StatusLabel tone={NODE_CONDITION_TONE[condition]}>
      {NODE_CONDITION_LABEL[condition]}
    </StatusLabel>
  )
}

/**
 * Cordon is observed schedulability, not health — it is rendered on its own
 * axis so a ready-but-cordoned node never reads as a healthy, usable node.
 */
export function CordonState({ cordoned }: { cordoned: boolean }) {
  if (!cordoned) {
    return (
      <span className="inline-flex items-center gap-2 font-mono text-xs text-muted-foreground/60">
        <StatusDot tone="neutral" />
        schedulable
      </span>
    )
  }
  return (
    <Badge
      variant="outline"
      className="border-amber-500/40 bg-amber-500/10 font-mono text-2xs uppercase tracking-wider text-amber-600 dark:text-amber-400"
    >
      cordoned
    </Badge>
  )
}

export function DeviceHealthLabel({ device }: { device: Device }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <StatusLabel tone={DEVICE_HEALTH_TONE[device.health]}>
        {device.health}
      </StatusLabel>
      {device.throttled && (
        <Badge
          variant="outline"
          className="border-amber-500/40 font-mono text-2xs text-amber-600 dark:text-amber-400"
        >
          throttled
        </Badge>
      )}
    </span>
  )
}

export function EccCell({ device }: { device: Device }) {
  const clean = device.ecc_errors.volatile === 0 && device.ecc_errors.aggregate === 0
  return (
    <span
      className={
        clean
          ? "font-mono text-2xs text-muted-foreground/60"
          : "font-mono text-2xs text-amber-600 dark:text-amber-400"
      }
    >
      {device.ecc_errors.volatile}/{device.ecc_errors.aggregate}
    </span>
  )
}

export function AllocationCell({
  allocation,
}: {
  allocation: DeviceAllocation | null
}) {
  if (!allocation) {
    return <span className="font-mono text-2xs text-muted-foreground/50">idle</span>
  }
  return (
    <div className="font-mono text-2xs leading-tight">
      <div>{allocation.deployment_name}</div>
      <div className="text-muted-foreground/60">
        {allocation.replica_id} · {allocation.project_name}
      </div>
    </div>
  )
}

export function TopologyBadge({
  domainId,
  kind,
}: {
  domainId: string | null
  kind: TopologyKind | null
}) {
  if (!domainId) {
    return <span className="font-mono text-2xs text-muted-foreground/50">—</span>
  }
  return (
    <Link
      to="/devices"
      search={{ domain: domainId }}
      className="inline-flex items-center gap-1.5 font-mono text-2xs hover:underline"
    >
      <span>{domainId}</span>
      {kind && (
        <span className="text-muted-foreground/50">
          {TOPOLOGY_KIND_LABEL[kind]}
        </span>
      )}
    </Link>
  )
}
