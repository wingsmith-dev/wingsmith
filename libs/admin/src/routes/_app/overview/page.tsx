import { PageHeader } from "@/components/layout/page-header"
import {
  useOpenOperations,
  useOverviewClusters,
  useOverviewDeployments,
  useOverviewDevices,
  useOverviewEndpoints,
  useOverviewNodes,
  useRecentEvents,
} from "@/modules/overview/queries"
import {
  summariseClusters,
  summariseDemand,
  summariseDevices,
  summariseFleet,
  summariseNodes,
} from "@/modules/overview/summary"
import { ClusterFleet } from "./ClusterFleet"
import { FleetHealth } from "./FleetHealth"
import { OpenOperations } from "./OpenOperations"
import { RecentEvents } from "./RecentEvents"
import { StatTile, type TileNote } from "./StatTile"

/**
 * Every number here is drawn from a drained list rather than a first page, so
 * the tiles are collection totals and not page sizes. The one exception is the
 * event feed, which is a recent slice by construction and says so.
 */
export function OverviewPage() {
  const clusters = useOverviewClusters()
  const nodes = useOverviewNodes()
  const devices = useOverviewDevices()
  const deployments = useOverviewDeployments()
  const endpoints = useOverviewEndpoints()
  const events = useRecentEvents()
  const operations = useOpenOperations()

  const clusterSummary = clusters.data && summariseClusters(clusters.data)
  const nodeSummary = nodes.data && summariseNodes(nodes.data)
  const deviceSummary = devices.data && summariseDevices(devices.data)
  const demandSummary =
    deployments.data && endpoints.data
      ? summariseDemand(deployments.data, endpoints.data)
      : undefined

  const fleet =
    clusters.data && nodes.data && devices.data && deployments.data
      ? summariseFleet(
          clusters.data,
          nodes.data,
          devices.data,
          deployments.data
        )
      : undefined

  const clusterNotes: TileNote[] = clusterSummary
    ? [
        { label: `${clusterSummary.active} active`, tone: "ok" },
        ...(clusterSummary.unreachable > 0
          ? [
              {
                label: `${clusterSummary.unreachable} unreachable`,
                tone: "danger" as const,
              },
            ]
          : []),
        ...(clusterSummary.outOfSync > 0
          ? [
              {
                label: `${clusterSummary.outOfSync} out of sync`,
                tone: "warn" as const,
              },
            ]
          : []),
      ]
    : []

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Overview"
        description="fleet health and what is running on it"
      />

      <div className="space-y-4 p-4">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <StatTile
            kind="cluster"
            label="Clusters"
            value={clusterSummary?.total ?? 0}
            to="/clusters"
            notes={clusterNotes}
            isLoading={clusters.isLoading}
            error={clusters.error}
          />
          <StatTile
            kind="node"
            label="Nodes"
            value={
              nodeSummary ? `${nodeSummary.ready}/${nodeSummary.total}` : 0
            }
            detail="ready"
            to="/nodes"
            notes={
              nodeSummary
                ? [
                    ...(nodeSummary.notReady > 0
                      ? [
                          {
                            label: `${nodeSummary.notReady} not ready`,
                            tone: "danger" as const,
                          },
                        ]
                      : []),
                    ...(nodeSummary.cordoned > 0
                      ? [
                          {
                            label: `${nodeSummary.cordoned} cordoned`,
                            tone: "warn" as const,
                          },
                        ]
                      : []),
                  ]
                : []
            }
            isLoading={nodes.isLoading}
            error={nodes.error}
          />
          <StatTile
            kind="device"
            label="Devices"
            value={
              deviceSummary
                ? `${deviceSummary.healthy}/${deviceSummary.total}`
                : 0
            }
            detail="healthy"
            to="/devices"
            notes={
              deviceSummary
                ? [
                    {
                      label: `${deviceSummary.allocatable} allocatable`,
                      tone: "ok" as const,
                    },
                    ...(deviceSummary.impaired.length > 0
                      ? [
                          {
                            label: `${deviceSummary.impaired.length} need attention`,
                            tone: "warn" as const,
                          },
                        ]
                      : []),
                  ]
                : []
            }
            isLoading={devices.isLoading}
            error={devices.error}
          />
          <StatTile
            kind="deployment"
            label="Deployments"
            value={demandSummary?.deployments ?? 0}
            to="/deployments"
            notes={
              demandSummary
                ? [
                    {
                      label: `${demandSummary.replicasReady}/${demandSummary.replicasDesired} replicas ready`,
                      tone:
                        demandSummary.replicasReady <
                        demandSummary.replicasDesired
                          ? ("warn" as const)
                          : ("ok" as const),
                    },
                    ...(demandSummary.deploymentsPending > 0
                      ? [
                          {
                            label: `${demandSummary.deploymentsPending} pending placement`,
                            tone: "info" as const,
                          },
                        ]
                      : []),
                  ]
                : []
            }
            isLoading={deployments.isLoading}
            error={deployments.error}
          />
          <StatTile
            kind="endpoint"
            label="Endpoints"
            value={demandSummary?.endpoints ?? 0}
            to="/endpoints"
            notes={
              demandSummary
                ? [
                    {
                      label: `${demandSummary.endpointsEnabled} enabled`,
                      tone: "ok" as const,
                    },
                  ]
                : []
            }
            isLoading={endpoints.isLoading}
            error={endpoints.error}
          />
        </div>

        <div className="grid gap-4 xl:grid-cols-2">
          <FleetHealth
            summary={deviceSummary}
            isLoading={devices.isLoading}
            error={devices.error}
          />
          <ClusterFleet
            rows={fleet}
            isLoading={
              clusters.isLoading ||
              nodes.isLoading ||
              devices.isLoading ||
              deployments.isLoading
            }
            error={
              clusters.error ??
              nodes.error ??
              devices.error ??
              deployments.error
            }
          />
        </div>

        <div className="grid gap-4 xl:grid-cols-2">
          <RecentEvents
            events={events.items}
            isLoading={events.isLoading}
            error={events.error}
          />
          <OpenOperations
            operations={operations.data}
            isLoading={operations.isLoading}
            error={operations.error}
          />
        </div>
      </div>
    </div>
  )
}
