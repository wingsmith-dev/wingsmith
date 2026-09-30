import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { CapacityRow } from "@/modules/overview/fleet"
import { Link } from "@tanstack/react-router"
import { Panel, PanelEmpty } from "./Panel"

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
            </div>
          )
        })}
      </div>
    </Panel>
  )
}
