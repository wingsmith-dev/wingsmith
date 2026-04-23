import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { CapacityRow } from "@/modules/overview/fleet"
import { Link } from "@tanstack/react-router"
import { Panel, PanelEmpty } from "./Panel"

/**
 * Replicas waiting on each accelerator, from the mockup.
 *
 * A deployment carries no device class — the shape lives on the recipe behind
 * the workload — so the console cannot attribute a pending replica to the
 * hardware it is waiting for without fetching every recipe on the page. The
 * fleet-wide unschedulable count in the stat row above is real; only this
 * per-class attribution is the mockup's own.
 *
 * TODO-API: pending demand per device class. Either `device_class` resolved onto
 * `DeploymentSchema`, or a placement-pressure rollup (e.g.
 * `GET /v1/device-classes/{name}/pressure` returning replicas pending on that
 * class) so the bar and the queue behind it come from the same read.
 */
const MOCK_PENDING_BY_CLASS: Record<string, number> = {
  "H100-80GB-SXM": 4,
}

function fillTone(percent: number): string {
  if (percent >= 95) return "bg-destructive"
  if (percent >= 85) return "bg-amber-500"
  return "bg-primary"
}

/**
 * Allocated against allocatable, per accelerator. The one number that predicts a
 * failed placement, which a healthy-device count never does: every device in a
 * fully allocated pool is healthy.
 */
export function CapacityPressure({
  rows,
  isLoading,
  error,
}: {
  rows: CapacityRow[] | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Panel
      title="Capacity pressure"
      description="allocated vs allocatable, by accelerator"
      isLoading={isLoading}
      error={error}
      errorMessage="Failed to load devices"
      action={
        <Button asChild variant="outline" size="sm">
          <Link to="/devices">All devices</Link>
        </Button>
      }
    >
      {rows && rows.length === 0 && (
        <PanelEmpty>No devices reported yet.</PanelEmpty>
      )}

      <div className="flex flex-col gap-3.5">
        {rows?.map((row) => {
          const pending = MOCK_PENDING_BY_CLASS[row.deviceClass]
          return (
            <div key={row.deviceClass} className="flex flex-col gap-1.5">
              <div className="flex items-baseline justify-between gap-2">
                <Link
                  to="/devices"
                  search={{ class: row.deviceClass }}
                  className="font-mono text-xs hover:underline"
                >
                  {row.deviceClass}
                </Link>
                <span className="font-mono text-2xs text-muted-foreground">
                  {row.allocated} / {row.total} · {row.percent}%
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-secondary">
                <div
                  className={cn("h-full rounded-full", fillTone(row.percent))}
                  style={{ width: `${row.percent}%` }}
                />
              </div>
              {pending !== undefined && (
                <span className="font-mono text-2xs text-amber-600 dark:text-amber-400">
                  {pending} replicas pending placement
                </span>
              )}
            </div>
          )
        })}
      </div>
    </Panel>
  )
}
