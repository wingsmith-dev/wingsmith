import type { StatusTone } from "@/components/layout/status-dot"
import { cn } from "@/lib/utils"
import type {
  Artifact,
  Deployment,
  DeploymentReplicaRollup,
  Endpoint,
  Workload,
} from "@/modules/demand/types"

/**
 * Whether this name can serve, which is not what `enabled` says.
 *
 * Read off the targets' replica rollups and never off the metrics store: an
 * endpoint nobody has called reports no series at all, so a measurement cannot
 * tell quiet from down, and a header dot that guesses is worse than one that
 * declines to answer. Until the deployment list has loaded there is no answer
 * and this says so instead of showing green.
 *
 * `enabled` is a separate reading and stays separate: declared and observed are
 * two different kinds of thing, and a name somebody turned off is
 * not a fault.
 */
export function servingHealth(
  endpoint: Endpoint,
  rollups: (DeploymentReplicaRollup | undefined)[]
): { tone: StatusTone; label: string } {
  if (endpoint.targets.length === 0) {
    return { tone: "danger", label: "Routes nowhere" }
  }
  const known = rollups.filter((r): r is DeploymentReplicaRollup => r != null)
  if (known.length === 0) return { tone: "neutral", label: "Unknown" }
  const ready = known.reduce((sum, r) => sum + r.ready, 0)
  const total = known.reduce((sum, r) => sum + r.total, 0)
  if (ready === 0) return { tone: "danger", label: "Down" }
  if (ready < total) return { tone: "warn", label: "Degraded" }
  return { tone: "ok", label: "Serving" }
}

/** A ready/total replica count with one cell per replica, ready cells lit. */
export function ReplicaBar({ ready, total }: { ready: number; total: number }) {
  if (total <= 0) {
    return <span className="text-muted-foreground">—</span>
  }
  const cells = Math.min(total, 12)
  return (
    <span className="inline-flex items-center gap-2">
      <span className="font-mono text-xs tabular-nums">
        {ready}/{total}
      </span>
      <span className="flex gap-0.5">
        {Array.from({ length: cells }, (_, i) => (
          <span
            key={i}
            className={cn(
              "h-3 w-[5px] rounded-[1px]",
              i < ready ? "bg-emerald-500" : "bg-destructive"
            )}
          />
        ))}
      </span>
    </span>
  )
}

/** `fp8 · awq` from a serving block's quantization and dtype, or null if neither is set. */
export function quantLabel(workload?: Workload): string | null {
  const parts = [
    workload?.serving.quantization,
    workload?.serving.dtype,
  ].filter((p): p is string => Boolean(p))
  return parts.length > 0 ? parts.join(" · ") : null
}

/** What one endpoint target resolves to across deployment → workload → artifact. */
export interface ResolvedTarget {
  deployment?: Deployment
  workload?: Workload
  artifact?: Artifact
  /** The cluster region this target lands in, off the workload's deployment row. */
  region?: string | null
}
