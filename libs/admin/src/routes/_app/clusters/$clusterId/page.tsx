import { Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatError } from "@/lib/format"
import {
  CLUSTER_PHASE_TONE,
  clusterLabel,
  humanize,
  syncTone,
} from "@/modules/clusters/display"
import { useCluster } from "@/modules/clusters/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { ComponentsTable } from "./ComponentsTable"
import { ClusterDevicesTable } from "./DevicesTable"
import { DeclaredCard } from "./HealthCard"
import { ClusterNodesTable } from "./NodesTable"
import { SyncCard } from "./SyncCard"
import { ClusterTopologyTable } from "./TopologyTable"

export function ClusterDetailPage() {
  const { clusterId } = useParams({ from: "/_app/clusters/$clusterId" })
  const { data: cluster, isLoading, error } = useCluster(clusterId)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Cluster" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load cluster")}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={cluster ? clusterLabel(cluster) : "Cluster"}
        description={cluster ? `${cluster.id} · ${cluster.region}` : undefined}
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
              <StatusLabel tone={CLUSTER_PHASE_TONE[cluster.phase] ?? "neutral"}>
                {humanize(cluster.phase)}
              </StatusLabel>
              <StatusLabel tone={cluster.reachable ? "ok" : "danger"}>
                {cluster.reachable ? "reachable" : "unreachable"}
              </StatusLabel>
              <StatusLabel tone={syncTone(cluster.sync.state)}>
                {humanize(cluster.sync.state)}
              </StatusLabel>
              <Labels labels={cluster.labels} />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <DeclaredCard cluster={cluster} />
              <SyncCard sync={cluster.sync} />
            </div>

            <Tabs defaultValue="nodes">
              <TabsList>
                <TabsTrigger value="nodes">Nodes</TabsTrigger>
                <TabsTrigger value="devices">Devices</TabsTrigger>
                <TabsTrigger value="topology">Topology</TabsTrigger>
                <TabsTrigger value="components">Components</TabsTrigger>
              </TabsList>
              <TabsContent value="nodes" className="mt-3">
                <ClusterNodesTable clusterId={cluster.id} />
              </TabsContent>
              <TabsContent value="devices" className="mt-3">
                <ClusterDevicesTable clusterId={cluster.id} />
              </TabsContent>
              <TabsContent value="topology" className="mt-3">
                <ClusterTopologyTable clusterId={cluster.id} />
              </TabsContent>
              <TabsContent value="components" className="mt-3">
                <ComponentsTable clusterId={cluster.id} />
              </TabsContent>
            </Tabs>
          </>
        )}
      </div>
    </div>
  )
}
