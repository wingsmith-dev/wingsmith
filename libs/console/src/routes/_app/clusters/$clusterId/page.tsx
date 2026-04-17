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
import { useState } from "react"
import { ComponentsTable } from "./ComponentsTable"
import { ClusterDevicesTable } from "./DevicesTable"
import { DeclaredCard } from "./HealthCard"
import { ClusterNodesTable } from "./NodesTable"
import { ClusterOverviewTab } from "./OverviewTab"
import { ClusterPoolsPanel } from "./PoolsPanel"
import { ServingCard } from "./ServingCard"
import { SyncCard } from "./SyncCard"
import { ClusterTopologyTable } from "./TopologyTable"

function TabNote({ children }: React.PropsWithChildren) {
  return (
    <p className="font-mono text-2xs leading-relaxed text-muted-foreground/60">
      {children}
    </p>
  )
}

export function ClusterDetailPage() {
  const { clusterId } = useParams({ from: "/_app/clusters/$clusterId" })
  const { data: cluster, isLoading, error } = useCluster(clusterId)
  const [tab, setTab] = useState("overview")
  const [creatingPool, setCreatingPool] = useState(false)

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
          <Button
            size="sm"
            onClick={() => {
              setTab("pools")
              setCreatingPool(true)
            }}
          >
            New pool
          </Button>
        }
        readActions={
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
              <StatusLabel
                tone={CLUSTER_PHASE_TONE[cluster.phase] ?? "neutral"}
              >
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

            <ServingCard cluster={cluster} />

            <Tabs value={tab} onValueChange={setTab}>
              <TabsList>
                <TabsTrigger value="overview">Overview</TabsTrigger>
                <TabsTrigger value="nodes">Nodes</TabsTrigger>
                <TabsTrigger value="pools">Pools</TabsTrigger>
                <TabsTrigger value="devices">Devices</TabsTrigger>
                <TabsTrigger value="topology">Topology</TabsTrigger>
                <TabsTrigger value="components">Components</TabsTrigger>
              </TabsList>
              <TabsContent value="overview" className="mt-3">
                <ClusterOverviewTab cluster={cluster} />
              </TabsContent>
              <TabsContent value="pools" className="mt-3 space-y-3">
                <TabNote>
                  Declared. A pool is the provisioning spec this cluster is
                  asked for; its node bounds are how more or fewer nodes get
                  requested.
                </TabNote>
                <ClusterPoolsPanel
                  clusterId={cluster.id}
                  creating={creatingPool}
                  onCreatingChange={setCreatingPool}
                />
              </TabsContent>
              <TabsContent value="nodes" className="mt-3 space-y-3">
                <TabNote>
                  Observed. Nodes are reported by the cluster and cannot be
                  added or removed here — raise a pool's node bounds instead,
                  and act on a node by draining or cordoning it.
                </TabNote>
                <ClusterNodesTable clusterId={cluster.id} />
              </TabsContent>
              <TabsContent value="devices" className="mt-3 space-y-3">
                <TabNote>
                  Observed. Devices arrive with the node that reports them; a
                  device is acted on by resetting it, never by editing it.
                </TabNote>
                <ClusterDevicesTable clusterId={cluster.id} />
              </TabsContent>
              <TabsContent value="topology" className="mt-3 space-y-3">
                <TabNote>
                  Observed. The cluster is the authority on topology.
                </TabNote>
                <ClusterTopologyTable clusterId={cluster.id} />
              </TabsContent>
              <TabsContent value="components" className="mt-3 space-y-3">
                <TabNote>
                  Reported, not managed. Hangar records what version of each
                  component a cluster is running and whether it is behind, but
                  it does not install or upgrade them — that is done out of band
                  by whatever provisions the cluster.
                </TabNote>
                <ComponentsTable clusterId={cluster.id} />
              </TabsContent>
            </Tabs>
          </>
        )}
      </div>
    </div>
  )
}
