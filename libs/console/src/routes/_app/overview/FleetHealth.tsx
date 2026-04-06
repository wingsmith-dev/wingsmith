import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { formatPercent, formatRelative } from "@/lib/format"
import { deviceHealthTone } from "@/modules/supply/display"
import type { DeviceSummary } from "@/modules/overview/summary"
import { Link } from "@tanstack/react-router"
import { Panel, PanelEmpty } from "./Panel"

/**
 * Health broken out by state, then every impaired device named individually.
 * A degraded, hardware-throttled accelerator is the one fact on this page that
 * an average would erase, so it gets a row of its own with its reason on it.
 */
export function FleetHealth({
  summary,
  isLoading,
  error,
}: {
  summary: DeviceSummary | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Panel
      title="Fleet health"
      description="observed · reported by the clusters"
      to="/devices"
      linkLabel="All devices"
      isLoading={isLoading}
      error={error}
      errorMessage="Failed to load devices"
      skeletonRows={5}
    >
      {summary && (
        <div className="divide-y">
          <div className="space-y-3 p-4">
            <div className="flex items-baseline justify-between">
              <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
                healthy devices
              </span>
              <span className="font-mono text-xs">
                {summary.healthy}/{summary.total} ·{" "}
                {formatPercent(summary.healthy, summary.total)}
              </span>
            </div>
            <Progress
              value={
                summary.total ? (summary.healthy / summary.total) * 100 : 0
              }
              className="h-1.5"
            />
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {summary.byHealth.map((entry) => (
                <Link
                  key={entry.health}
                  to="/devices"
                  search={{ health: entry.health }}
                  className="inline-flex items-center gap-2 font-mono text-2xs hover:underline"
                >
                  <StatusDot tone={deviceHealthTone(entry.health)} />
                  {entry.health}
                  <span className="text-muted-foreground/60">
                    {entry.count}
                  </span>
                </Link>
              ))}
              <span className="ml-auto font-mono text-2xs text-muted-foreground/60">
                {summary.allocatable} allocatable
              </span>
            </div>
          </div>

          <div className="space-y-2 p-4">
            <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
              by device class
            </div>
            {summary.byClass.map((entry) => (
              <div
                key={entry.deviceClass}
                className="flex items-baseline justify-between gap-3 font-mono text-2xs"
              >
                <Link
                  to="/devices"
                  search={{ class: entry.deviceClass }}
                  className="hover:underline"
                >
                  {entry.deviceClass}
                </Link>
                <span
                  className={
                    entry.healthy === entry.count
                      ? "text-muted-foreground/60"
                      : "text-amber-600 dark:text-amber-400"
                  }
                >
                  {entry.healthy}/{entry.count} healthy
                </span>
              </div>
            ))}
          </div>

          <div className="space-y-2 p-4">
            <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
              needs attention
            </div>
            {summary.impaired.length === 0 ? (
              <PanelEmpty>
                Every device reports healthy and unthrottled.
              </PanelEmpty>
            ) : (
              summary.impaired.map((device) => (
                <Link
                  key={device.id}
                  to="/devices/$deviceId"
                  params={{ deviceId: device.id }}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 hover:bg-amber-500/10"
                >
                  <StatusLabel tone={deviceHealthTone(device.health)}>
                    {device.health}
                  </StatusLabel>
                  <span className="font-mono text-2xs">
                    {device.node_name}
                    <span className="text-muted-foreground/50">
                      {" "}
                      · gpu {device.device_index} · {device.device_class}
                    </span>
                  </span>
                  {device.throttled &&
                    (device.throttle_reasons.length
                      ? device.throttle_reasons
                      : ["throttled"]
                    ).map((reason) => (
                      <Badge
                        key={reason}
                        variant="outline"
                        className="border-amber-500/40 font-mono text-2xs text-amber-600 dark:text-amber-400"
                      >
                        {reason}
                      </Badge>
                    ))}
                  {device.health_reason && (
                    <span className="font-mono text-2xs text-muted-foreground">
                      {device.health_reason}
                    </span>
                  )}
                  <span className="ml-auto font-mono text-2xs text-muted-foreground/50">
                    {device.cluster_name} · {formatRelative(device.observed_at)}
                  </span>
                </Link>
              ))
            )}
          </div>
        </div>
      )}
    </Panel>
  )
}
