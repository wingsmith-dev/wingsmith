import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatPercent, formatRelative } from "@/lib/format"
import {
  CLUSTER_PHASE_TONE,
  SYNC_LABEL,
  SYNC_TONE,
  humanize,
} from "@/modules/clusters/display"
import { useClusters } from "@/modules/clusters/queries"
import { Link } from "@tanstack/react-router"

export function ClustersPage() {
  const { data, isLoading, error } = useClusters()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Clusters"
        description="observed · enrolment, capacity and operator sync"
      />

      <div className="p-4">
        <div className="overflow-hidden rounded-lg border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Cluster</TableHead>
                <TableHead>Region</TableHead>
                <TableHead>Phase</TableHead>
                <TableHead>Reachability</TableHead>
                <TableHead>Sync</TableHead>
                <TableHead className="text-right">Nodes</TableHead>
                <TableHead className="text-right">Devices</TableHead>
                <TableHead className="text-right">Allocated</TableHead>
                <TableHead>Device classes</TableHead>
                <TableHead className="text-right">Last sync</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading &&
                Array.from({ length: 4 }, (_, i) => (
                  <TableRow key={i}>
                    <TableCell colSpan={10}>
                      <Skeleton className="h-5 w-full" />
                    </TableCell>
                  </TableRow>
                ))}

              {error && (
                <TableRow>
                  <TableCell
                    colSpan={10}
                    className="py-10 text-center text-sm text-destructive"
                  >
                    {error instanceof Error ? error.message : "Failed to load"}
                  </TableCell>
                </TableRow>
              )}

              {data?.items.map((cluster) => (
                <TableRow key={cluster.id} className="hover:bg-accent/40">
                  <TableCell>
                    <Link
                      to="/clusters/$clusterId"
                      params={{ clusterId: cluster.id }}
                      className="block"
                    >
                      <span className="font-medium">
                        {cluster.display_name}
                      </span>
                      <span className="ml-2 font-mono text-2xs text-muted-foreground/60">
                        {cluster.name}
                      </span>
                    </Link>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {cluster.region}
                  </TableCell>
                  <TableCell>
                    <StatusLabel tone={CLUSTER_PHASE_TONE[cluster.phase]}>
                      {humanize(cluster.phase)}
                    </StatusLabel>
                  </TableCell>
                  <TableCell>
                    <StatusLabel tone={cluster.reachable ? "ok" : "danger"}>
                      {cluster.reachable ? "reachable" : "unreachable"}
                    </StatusLabel>
                  </TableCell>
                  <TableCell>
                    <StatusLabel
                      tone={SYNC_TONE[cluster.sync.state]}
                      pulse={cluster.sync.state === "connected"}
                    >
                      {SYNC_LABEL[cluster.sync.state]}
                    </StatusLabel>
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {cluster.capacity.nodes_ready}/{cluster.capacity.nodes_total}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {cluster.capacity.devices_healthy}/
                    {cluster.capacity.devices_total}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {formatPercent(
                      cluster.capacity.devices_allocated,
                      cluster.capacity.devices_total
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {cluster.device_classes.length === 0 && (
                        <span className="font-mono text-2xs text-muted-foreground/50">
                          none
                        </span>
                      )}
                      {cluster.device_classes.map((deviceClass) => (
                        <Badge
                          key={deviceClass}
                          variant="secondary"
                          className="font-mono text-2xs"
                        >
                          {deviceClass}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell className="text-right font-mono text-2xs text-muted-foreground/70">
                    {formatRelative(cluster.sync.last_sync_at)}
                  </TableCell>
                </TableRow>
              ))}

              {data && data.items.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={10}
                    className="py-10 text-center text-sm text-muted-foreground"
                  >
                    No clusters enrolled.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  )
}
