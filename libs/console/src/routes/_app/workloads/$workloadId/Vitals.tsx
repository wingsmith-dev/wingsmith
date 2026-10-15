import { StatusDot, type StatusTone } from "@/components/layout/status-dot"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import {
  parallelismLabel,
  type OperationalStatus,
} from "@/modules/demand/display"
import { groupDevices, scaleUnitFor } from "@/modules/demand/topology"
import type { Workload } from "@/modules/demand/types"

/**
 * The four facts the page is opened for, on one line above everything else.
 *
 * One strip, one row, no cards inside a card, so "is this healthy, and how big
 * is it" is answered at a glance rather than assembled by reading.
 */
export function Vitals({
  workload,
  operational,
}: {
  workload: Workload
  operational: OperationalStatus
}) {
  const clusters = workload.deployments.map((d) => d.cluster_name)
  const attention = [operational.reason, operational.placement]
    .filter(Boolean)
    .join(" · ")
  return (
    <Card className="grid grid-cols-2 divide-y py-0 sm:grid-cols-4 sm:divide-x sm:divide-y-0">
      <Vital
        label="Status"
        value={
          <span
            className="inline-flex items-center gap-2"
            title={attention || undefined}
          >
            <StatusDot
              tone={operational.tone}
              pulse={operational.tone === "danger"}
            />
            {operational.phase}
          </span>
        }
        detail={operational.placement}
        tone={operational.needsAttention ? operational.tone : undefined}
      />
      <Vital
        label={scaleUnitFor(workload)}
        value={`${workload.scale.ready}/${workload.scale.desired}`}
        detail={`min ${workload.scale.min} · max ${workload.scale.max}`}
      />
      <Vital
        label="Clusters"
        value={String(workload.deployments.length)}
        detail={clusters.join(", ") || "none placed"}
      />
      {/* Devices per WORKER, always. The group's total is derived beside it
          and never replaces it: `device_count` is a digest field and a tile
          that quietly showed 24 where the column says 8 would be the same
          reinterpretation the group count exists to prevent. */}
      <Vital
        label="Hardware"
        value={`${workload.serving.shape.device_count}× ${workload.serving.shape.device_class}`}
        detail={
          groupDevices(workload)
            ? `${parallelismLabel(workload.serving.parallelism)} · ${groupDevices(workload)} per group`
            : parallelismLabel(workload.serving.parallelism)
        }
      />
    </Card>
  )
}

export function Vital({
  label,
  value,
  detail,
  tone,
}: {
  label: string
  value: React.ReactNode
  detail?: string
  tone?: StatusTone
}) {
  return (
    <div className="min-w-0 px-4 py-3">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        {label}
      </div>
      <div className="mt-2 truncate font-mono text-base leading-none">
        {value}
      </div>
      <div
        className={cn(
          "mt-2 truncate font-mono text-2xs",
          tone === "danger"
            ? "text-destructive"
            : tone === "warn"
              ? "text-warning"
              : "text-muted-foreground"
        )}
      >
        {detail}
      </div>
    </div>
  )
}
