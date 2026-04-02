import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { deviceHealthTone, nodeConditionTone } from "@/modules/supply/display"
import type { Device } from "@/modules/supply/types"

export function NodeConditionLabel({ condition }: { condition: string }) {
  return (
    <StatusLabel tone={nodeConditionTone(condition)}>
      {condition.replace(/_/g, " ")}
    </StatusLabel>
  )
}

/**
 * Cordon is observed schedulability, not health, and gets its own axis: a
 * ready-but-cordoned node must never read as a healthy, usable node.
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
      className="border-amber-500/40 bg-amber-500/10 font-mono text-2xs tracking-wider text-amber-600 uppercase dark:text-amber-400"
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
          className="border-amber-500/40 font-mono text-2xs text-amber-600 dark:text-amber-400"
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
    return (
      <span className="font-mono text-2xs text-muted-foreground/40">off</span>
    )
  }
  const clean =
    device.ecc_volatile_errors === 0 && device.ecc_aggregate_errors === 0
  return (
    <span
      className={
        clean
          ? "font-mono text-2xs text-muted-foreground/60"
          : "font-mono text-2xs text-amber-600 dark:text-amber-400"
      }
    >
      {device.ecc_volatile_errors}/{device.ecc_aggregate_errors}
      {device.ecc_pending_retirement && " ⚑"}
    </span>
  )
}

/**
 * Allocatability is the device's own observed flag. What actually holds it is a
 * different question, answered by `/allocations?device=` and not by this row —
 * the device schema carries no allocation object.
 */
export function AllocatableCell({ allocatable }: { allocatable: boolean }) {
  return (
    <span
      className={
        allocatable
          ? "font-mono text-2xs text-emerald-600 dark:text-emerald-400"
          : "font-mono text-2xs text-muted-foreground/50"
      }
    >
      {allocatable ? "allocatable" : "unschedulable"}
    </span>
  )
}
