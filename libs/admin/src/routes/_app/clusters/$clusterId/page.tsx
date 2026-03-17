import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatAbsolute } from "@/lib/format"
import { CLUSTER_PHASE_TONE, humanize } from "@/modules/clusters/display"
import { useCluster } from "@/modules/clusters/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { ComponentsTable } from "./ComponentsTable"
import { DevicesTable } from "./DevicesTable"
import { HealthCard } from "./HealthCard"
import { NodesTable } from "./NodesTable"
import { TopologyTable } from "./TopologyTable"
import { SyncCard } from "./SyncCard"

export function ClusterDetailPage() {
  const { clusterId } = useParams({ from: "/_app/clusters/$clusterId" })
  const { data: cluster, isLoading, error } = useCluster(clusterId)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Cluster" />
        <div className="p-10 text-center text-sm text-destructive">
          {error instanceof Error ? error.message : "Failed to load cluster"}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={cluster?.display_name ?? "Cluster"}
        description={cluster ? `${cluster.name} · ${cluster.region}` : undefined}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to="/clusters">
              <IconArrowLeft className="size-4" />
              All clusters
            </Link>
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {cluster && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <StatusLabel tone={CLUSTER_PHASE_TONE[cluster.phase]}>
                {humanize(cluster.phase)}
              </StatusLabel>
              <StatusLabel tone={cluster.reachable ? "ok" : "danger"}>
                {cluster.reachable ? "reachable" : "unreachable"}
              </StatusLabel>
              <span className="font-mono text-2xs text-muted-foreground/60">
                enrolled {formatAbsolute(cluster.enrolled_at)}
              </span>
              <div className="flex flex-wrap gap-1">
                {Object.entries(cluster.labels).map(([key, value]) => (
                  <Badge
                    key={key}
                    variant="outline"
                    className="font-mono text-2xs"
                  >
                    {key}={value}
                  </Badge>
                ))}
              </div>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <HealthCard capacity={cluster.capacity} />
              <SyncCard sync={cluster.sync} />
            </div>

            <Tabs defaultValue="nodes">
              <TabsList>
                <TabsTrigger value="nodes">
                  Nodes
                  <span className="ml-1.5 font-mono text-2xs text-muted-foreground/60">
                    {cluster.capacity.nodes_total}
                  </span>
                </TabsTrigger>
                <TabsTrigger value="devices">
                  Devices
                  <span className="ml-1.5 font-mono text-2xs text-muted-foreground/60">
                    {cluster.capacity.devices_total}
                  </span>
                </TabsTrigger>
                <TabsTrigger value="topology">
                  Topology
                  <span className="ml-1.5 font-mono text-2xs text-muted-foreground/60">
                    {cluster.topology_domain_count}
                  </span>
                </TabsTrigger>
                <TabsTrigger value="components">
                  Components
                  <span className="ml-1.5 font-mono text-2xs text-muted-foreground/60">
                    {cluster.components.length}
                  </span>
                </TabsTrigger>
              </TabsList>
              <TabsContent value="nodes" className="mt-3">
                <NodesTable clusterId={clusterId} />
              </TabsContent>
              <TabsContent value="devices" className="mt-3">
                <DevicesTable clusterId={clusterId} />
              </TabsContent>
              <TabsContent value="topology" className="mt-3">
                <TopologyTable clusterId={clusterId} />
              </TabsContent>
              <TabsContent value="components" className="mt-3">
                <ComponentsTable components={cluster.components} />
              </TabsContent>
            </Tabs>
          </>
        )}
      </div>
    </div>
  )
}
