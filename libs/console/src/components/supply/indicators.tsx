import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { deviceHealthTone, nodeConditionTone } from "@/modules/supply/display"
import type { Device } from "@/modules/supply/types"

/**
 * Node condition, or `stale` when the node's cluster agent is unreachable. A
 * cluster we cannot reach stopped reporting, so `ready` is only the last thing
 * it said: presenting it as a live fact would claim knowledge the plane does
 * not have. Stale wins over the stored condition and reads muted, not green.
 */
export function NodeConditionLabel({
  condition,
  stale = false,
}: {
  condition: string
  stale?: boolean
}) {
  if (stale) {
    return (
      <span
        title="This cluster's agent is unreachable; the last reported condition can no longer be confirmed."
        className="text-muted-foreground"
      >
        <StatusLabel tone="neutral">
          stale · {condition.replace(/_/g, " ")}
        </StatusLabel>
      </span>
    )
  }
  return (
    <StatusLabel tone={nodeConditionTone(condition)}>
      {condition.replace(/_/g, " ")}
    </StatusLabel>
  )
}

/**
 * GPU node or plain compute, told apart by the only thing that separates them.
 *
 * The plane records no role for a node: a compute node is a node with no
 * devices, and the table said so with a `0/0` and a dash, which reads as a GPU
 * node whose devices have gone missing. The badge states which of the two a row
 * is before the numbers are read at all.
 */
export function NodeKindBadge({ devices }: { devices: number }) {
  const gpu = devices > 0
  return (
    <Badge
      variant="outline"
      className={
        gpu
          ? "border-primary/40 bg-primary/10 font-mono text-2xs tracking-wider text-primary uppercase"
          : "font-mono text-2xs tracking-wider text-muted-foreground uppercase"
      }
    >
      {gpu ? "gpu" : "cpu"}
    </Badge>
  )
}

/**
 * Cordon is observed schedulability, not health, and gets its own axis: a
 * ready-but-cordoned node must never read as a healthy, usable node.
 */
export function CordonState({ cordoned }: { cordoned: boolean }) {
  if (!cordoned) {
    return (
      <span className="inline-flex items-center gap-2 font-mono text-xs text-muted-foreground">
        <StatusDot tone="neutral" />
        schedulable
      </span>
    )
  }
  return (
    <Badge
      variant="outline"
      className="border-amber-500/40 bg-amber-500/10 font-mono text-2xs tracking-wider text-warning uppercase"
    >
      cordoned
    </Badge>
  )
}

export function DeviceHealthLabel({ device }: { device: Device }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <StatusLabel tone={deviceHealthTone(device.health)}>
        {device.health}
      </StatusLabel>
      {device.throttled && (
        <Badge
          variant="outline"
          className="border-amber-500/40 font-mono text-2xs text-warning"
        >
          throttled
        </Badge>
      )}
    </span>
  )
}

/**
 * ECC counters, volatile over aggregate. `ecc_pending_retirement` is called out
 * separately because a page pending retirement is a device that is about to
 * stop being schedulable, not merely one with a history.
 */
export function EccCell({ device }: { device: Device }) {
  if (!device.ecc_enabled) {
    return <span className="font-mono text-2xs text-muted-foreground">off</span>
  }
  const clean =
    device.ecc_volatile_errors === 0 && device.ecc_aggregate_errors === 0
  return (
    <span
      className={
        clean
          ? "font-mono text-2xs text-muted-foreground"
          : "font-mono text-2xs text-warning"
      }
    >
      {device.ecc_volatile_errors}/{device.ecc_aggregate_errors}
      {device.ecc_pending_retirement && " ⚑"}
    </span>
  )
}

/**
 * Allocatability is the device's own observed flag. What actually holds it is a
 * different question, answered by `/allocations?device=` and not by this row:
 * the device schema carries no allocation object.
 */
export function AllocatableCell({ allocatable }: { allocatable: boolean }) {
  return (
    <span
      className={
        allocatable
          ? "font-mono text-2xs text-success"
          : "font-mono text-2xs text-muted-foreground"
      }
    >
      {allocatable ? "allocatable" : "unschedulable"}
    </span>
  )
}
