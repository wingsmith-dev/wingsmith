import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { formatRelative } from "@/lib/format"
import {
  CLUSTER_PHASE_TONE,
  clusterLabel,
  humanize,
  syncTone,
} from "@/modules/clusters/display"
import type { ClusterFleetRow } from "@/modules/overview/summary"
import { Link } from "@tanstack/react-router"
import { Panel, PanelEmpty } from "./Panel"

/**
 * Phase and reachability sit in the same row but never in the same column: a
 * cluster can be declared `active` and be unreachable at the same time, and one
 * merged status would hide exactly that case.
 */
export function ClusterFleet({
  rows,
  isLoading,
  error,
}: {
  rows: ClusterFleetRow[] | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Panel
      title="Clusters"
      description="declared phase · observed link"
      to="/clusters"
      linkLabel="All clusters"
      isLoading={isLoading}
      error={error}
      errorMessage="Failed to load clusters"
      skeletonRows={3}
    >
      {rows && rows.length === 0 && (
        <PanelEmpty>No clusters enrolled.</PanelEmpty>
      )}

      <div className="divide-y">
        {rows?.map(({ cluster, nodes, devices, deployments }) => (
          <div key={cluster.id} className="space-y-2 px-4 py-3">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <Link
                to="/clusters/$clusterId"
                params={{ clusterId: cluster.id }}
                className="text-sm font-medium hover:underline"
              >
                {clusterLabel(cluster)}
              </Link>
              <span className="font-mono text-2xs text-muted-foreground/60">
                {cluster.region}
                {cluster.zone && ` · ${cluster.zone}`}
              </span>
              <div className="ml-auto flex flex-wrap items-center gap-3">
                <StatusLabel tone={CLUSTER_PHASE_TONE[cluster.phase]}>
                  {cluster.phase}
                </StatusLabel>
                <StatusLabel tone={syncTone(cluster.sync.state)}>
                  {humanize(cluster.sync.state)}
                </StatusLabel>
                {!cluster.reachable && (
                  <Badge
                    variant="outline"
                    className="border-destructive/40 font-mono text-2xs text-destructive"
                  >
                    unreachable
                  </Badge>
                )}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-2xs text-muted-foreground">
              <Link
                to="/nodes"
                search={{ cluster: cluster.id }}
                className="hover:underline"
              >
                {nodes.ready}/{nodes.total} nodes ready
              </Link>
              {nodes.cordoned > 0 && (
                <Link
                  to="/nodes"
                  search={{ cluster: cluster.id, cordoned: "true" }}
                  className="text-amber-600 hover:underline dark:text-amber-400"
                >
                  {nodes.cordoned} cordoned
                </Link>
              )}
              <Link
                to="/devices"
                search={{ cluster: cluster.id }}
                className="hover:underline"
              >
                {devices.healthy}/{devices.total} devices healthy
              </Link>
              <Link
                to="/deployments"
                search={{ cluster: cluster.id }}
                className="hover:underline"
              >
                {deployments} deployments
              </Link>
              <span className="ml-auto text-muted-foreground/50">
                last seen {formatRelative(cluster.sync.last_seen_at)}
              </span>
            </div>

            {cluster.sync.last_error && (
              <p className="font-mono text-2xs text-destructive">
                {cluster.sync.last_error}
              </p>
            )}
          </div>
        ))}
      </div>
    </Panel>
  )
}
