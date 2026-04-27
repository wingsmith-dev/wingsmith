import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatError } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { useCluster } from "@/modules/clusters/queries"
import {
  CLUSTER_STAGE_META,
  clusterStage,
  type ClusterStage,
} from "@/modules/clusters/stage"
import type { Cluster } from "@/modules/clusters/types"
import {
  IconArrowLeft,
  IconCpu,
  IconLayoutDashboard,
  IconServer2,
  IconStack2,
  IconTerminal2,
  IconTopologyStar3,
} from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { useState } from "react"
import { ComponentsTable } from "./ComponentsTable"
import { ClusterDevicesTable } from "./DevicesTable"
import { ClusterIdentity } from "./Glance"
import { ClusterNodesTable } from "./NodesTable"
import { ClusterOverviewTab } from "./OverviewTab"
import { ConnectClusterCard } from "./ConnectClusterCard"
import { ClusterPoolsPanel } from "./PoolsPanel"
import { ClusterTopologyTable } from "./TopologyTable"

function TabNote({ children }: React.PropsWithChildren) {
  return (
    <p className="font-mono text-2xs leading-relaxed text-muted-foreground/60">
      {children}
    </p>
  )
}

/**
 * The stage badge on its own, for the stages that render no identity card — the
 * card carries stage, phase, reachability, sync and labels everywhere else, and
 * repeating them under the header only reads as noise.
 */
function StageRow({ stage }: { stage: ClusterStage }) {
  const meta = CLUSTER_STAGE_META[stage]
  return (
    <div className="flex flex-wrap items-center gap-3">
      <StatusLabel tone={meta.tone}>{meta.label}</StatusLabel>
    </div>
  )
}

/**
 * A one-line explainer shown for every stage except the unremarkable `live` and
 * `onboarding`, where {@link ConnectClusterCard} says the same thing at length
 * and directly above the commands that act on it.
 */
function StageBanner({ stage }: { stage: ClusterStage }) {
  if (stage === "live" || stage === "onboarding") return null
  const meta = CLUSTER_STAGE_META[stage]
  const tone =
    meta.tone === "warn"
      ? "border-amber-500/40 bg-amber-500/5"
      : meta.tone === "info"
        ? "border-primary/30 bg-primary/5"
        : "border-border bg-muted/30"
  return (
    <Card size="sm" className={`px-4 py-3 ${tone}`}>
      <p className="text-xs leading-relaxed text-muted-foreground">
        {meta.blurb}
      </p>
    </Card>
  )
}

function ViewTab({
  value,
  icon: Icon,
  children,
}: React.PropsWithChildren<{
  value: string
  icon: typeof IconLayoutDashboard
}>) {
  return (
    <TabsTrigger value={value}>
      <Icon />
      {children}
    </TabsTrigger>
  )
}

/**
 * The full operational dashboard — shown once a cluster is enrolled. Reused for
 * `live`, `degraded` and `draining`; the stage banner inside it carries the
 * caveat (stale link, draining) so the panels themselves stay the same.
 *
 * The tab row is the only thing here that escapes the page padding: it sits
 * flush under the header so its bottom border spans the full width.
 */
function ClusterDashboard({
  cluster,
  stage,
  tab,
  setTab,
  creatingPool,
  setCreatingPool,
}: {
  cluster: Cluster
  stage: ClusterStage
  tab: string
  setTab: (v: string) => void
  creatingPool: boolean
  setCreatingPool: (v: boolean) => void
}) {
  return (
    <Tabs value={tab} onValueChange={setTab} className="flex-col gap-0">
      <TabsList variant="underline" className="px-4">
        <ViewTab value="overview" icon={IconLayoutDashboard}>
          Overview
        </ViewTab>
        <ViewTab value="nodes" icon={IconServer2}>
          Nodes
        </ViewTab>
        <ViewTab value="pools" icon={IconStack2}>
          Pools
        </ViewTab>
        <ViewTab value="devices" icon={IconCpu}>
          Devices
        </ViewTab>
        <ViewTab value="topology" icon={IconTopologyStar3}>
          Topology
        </ViewTab>
        <ViewTab value="components" icon={IconTerminal2}>
          Components
        </ViewTab>
      </TabsList>

      <div className="space-y-4 p-4">
        <StageBanner stage={stage} />

        <TabsContent value="overview">
          <ClusterOverviewTab cluster={cluster} />
        </TabsContent>
        <TabsContent value="pools" className="space-y-3">
          <TabNote>
            Declared. A pool is the provisioning spec this cluster is asked for;
            its node bounds are how more or fewer nodes get requested.
          </TabNote>
          <ClusterPoolsPanel
            clusterId={cluster.id}
            creating={creatingPool}
            onCreatingChange={setCreatingPool}
          />
        </TabsContent>
        <TabsContent value="nodes" className="space-y-3">
          <TabNote>
            Observed. Nodes are reported by the cluster and cannot be added or
            removed here — raise a pool's node bounds instead, and act on a node
            by draining or cordoning it.
          </TabNote>
          <ClusterNodesTable clusterId={cluster.id} />
        </TabsContent>
        <TabsContent value="devices" className="space-y-3">
          <TabNote>
            Observed. Devices arrive with the node that reports them; a device
            is acted on by resetting it, never by editing it.
          </TabNote>
          <ClusterDevicesTable clusterId={cluster.id} />
        </TabsContent>
        <TabsContent value="topology" className="space-y-3">
          <TabNote>Observed. The cluster is the authority on topology.</TabNote>
          <ClusterTopologyTable clusterId={cluster.id} />
        </TabsContent>
        <TabsContent value="components" className="space-y-3">
          <TabNote>
            Reported, not managed. Hangar records what version of each component
            a cluster is running and whether it is behind, but it does not
            install or upgrade them — that is done out of band by whatever
            provisions the cluster.
          </TabNote>
          <ComponentsTable clusterId={cluster.id} />
        </TabsContent>
      </div>
    </Tabs>
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

  const stage = cluster ? clusterStage(cluster) : null
  const schedulable = stage === "live" || stage === "degraded"

  return (
    <div className="flex flex-col">
      <PageHeader
        title={cluster ? clusterLabel(cluster) : "Cluster"}
        description={cluster ? `${cluster.id} · ${cluster.region}` : undefined}
        actions={
          schedulable ? (
            <Button
              onClick={() => {
                setTab("pools")
                setCreatingPool(true)
              }}
            >
              New pool
            </Button>
          ) : undefined
        }
        readActions={
          <Button asChild variant="outline">
            <Link to="/clusters">
              <IconArrowLeft className="size-4" />
              All clusters
            </Link>
          </Button>
        }
      />

      {isLoading && (
        <div className="p-4">
          <Skeleton className="h-40 w-full" />
        </div>
      )}

      {cluster &&
        stage &&
        (stage === "live" || stage === "degraded" || stage === "draining" ? (
          <ClusterDashboard
            cluster={cluster}
            stage={stage}
            tab={tab}
            setTab={setTab}
            creatingPool={creatingPool}
            setCreatingPool={setCreatingPool}
          />
        ) : (
          <div className="space-y-4 p-4">
            {stage === "onboarding" && <StageRow stage={stage} />}
            <StageBanner stage={stage} />

            {stage === "onboarding" && (
              <div className="mx-auto w-full max-w-3xl">
                <ConnectClusterCard cluster={cluster} />
              </div>
            )}

            {stage === "archived" && (
              <div className="grid gap-4 lg:grid-cols-3">
                <ClusterIdentity cluster={cluster} />
              </div>
            )}
          </div>
        ))}
    </div>
  )
}
