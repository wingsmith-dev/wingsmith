import { StatusDot } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { clusterLabel } from "@/modules/clusters/display"
import type { ReachabilityRow } from "@/modules/overview/fleet"
import { Link } from "@tanstack/react-router"
import { Panel, PanelEmpty } from "./Panel"

/**
 * Partly here to caveat everything above it. Every observed count on this page is
 * only as fresh as the cluster that reported it, so the fleet's reconcile ages
 * sit on the same screen as the numbers they qualify.
 */
export function ClusterReachability({
  rows,
  isLoading,
  error,
}: {
  rows: ReachabilityRow[] | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Panel
      title="Clusters"
      description="reachability & last reconcile"
      isLoading={isLoading}
      error={error}
      errorMessage="Failed to load clusters"
      skeletonRows={5}
      action={
        <Button asChild variant="outline" size="sm">
          <Link to="/clusters">All</Link>
        </Button>
      }
    >
      {rows && rows.length === 0 && (
        <PanelEmpty>No clusters enrolled.</PanelEmpty>
      )}

      <div className="divide-y">
        {rows?.map((row) => (
          <div key={row.cluster.id} className="flex items-center gap-2.5 py-2">
            <StatusDot tone={row.tone} />
            <Link
              to="/clusters/$clusterId"
              params={{ clusterId: row.cluster.id }}
              className="min-w-0 flex-1 truncate text-xs hover:underline"
            >
              {clusterLabel(row.cluster)}
            </Link>
            <span
              className={cn(
                "shrink-0 font-mono text-2xs",
                row.stale
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-muted-foreground"
              )}
            >
              {row.note}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  )
}
