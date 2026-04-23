import { PageHeader } from "@/components/layout/page-header"
import { clusterLabel } from "@/modules/clusters/display"
import { deriveFleetAttention } from "@/modules/overview/attention"
import {
  allocationTotals,
  capacityByClass,
  reachabilityRows,
  replicaFootprint,
  staleClusters,
  summariseEndpointHealth,
  unschedulableReplicas,
} from "@/modules/overview/fleet"
import {
  useOverviewClusters,
  useOverviewDeployments,
  useOverviewDevices,
  useOverviewEndpoints,
  useOverviewNodes,
  useOverviewWorkloads,
  useRecentOperations,
  useUndecidedDiscovered,
} from "@/modules/overview/queries"
import { IconEye } from "@tabler/icons-react"
import * as React from "react"
import { CapacityPressure } from "./CapacityPressure"
import { ClusterReachability } from "./ClusterReachability"
import { NeedsAttention } from "./NeedsAttention"
import { RecentOperations } from "./RecentOperations"
import { SegTabs } from "./SegTabs"
import {
  ServingTraffic,
  TRAFFIC_WINDOWS,
  type TrafficWindow,
} from "./ServingTraffic"
import { StatCard } from "./StatCard"

/** Above this share of the fleet allocated, a placement is a coin toss. */
const TIGHT_PERCENT = 75
const FULL_PERCENT = 90

/**
 * The fleet page, ordered as the questions are asked: what is broken, what is
 * tight, what changed. Every number is drawn from a drained list rather than a
 * first page, so the cards are collection totals and not page sizes — the
 * operations feed is the one recent slice, and it says so.
 */
export function OverviewPage() {
  const [window, setWindow] = React.useState<TrafficWindow>("24h")

  const clusters = useOverviewClusters()
  const nodes = useOverviewNodes()
  const devices = useOverviewDevices()
  const deployments = useOverviewDeployments()
  const endpoints = useOverviewEndpoints()
  const workloads = useOverviewWorkloads()
  const discovered = useUndecidedDiscovered()
  const operations = useRecentOperations()

  const serving =
    endpoints.data && deployments.data
      ? summariseEndpointHealth(endpoints.data, deployments.data)
      : undefined
  const footprint = deployments.data && replicaFootprint(deployments.data)
  const allocation = devices.data && allocationTotals(devices.data)
  const pending = deployments.data && unschedulableReplicas(deployments.data)
  const capacity = devices.data && capacityByClass(devices.data)
  const reachability = clusters.data && reachabilityRows(clusters.data)
  const stale = clusters.data ? staleClusters(clusters.data) : []

  const attention =
    clusters.data ||
    devices.data ||
    nodes.data ||
    deployments.data ||
    endpoints.data ||
    discovered.data
      ? deriveFleetAttention({
          clusters: clusters.data,
          nodes: nodes.data,
          devices: devices.data,
          deployments: deployments.data,
          endpoints: endpoints.data,
          discovered: discovered.data,
        })
      : undefined

  const attentionLoading =
    clusters.isLoading ||
    devices.isLoading ||
    nodes.isLoading ||
    deployments.isLoading ||
    endpoints.isLoading ||
    discovered.isLoading

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Overview"
        description="what is broken, what is tight, what changed"
        readActions={
          <SegTabs
            aria-label="Traffic window"
            value={window}
            onChange={setWindow}
            options={TRAFFIC_WINDOWS.map((value) => ({ value, label: value }))}
          />
        }
      />

      <div className="flex flex-col gap-5 p-4">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            label="Endpoints serving"
            value={serving?.serving ?? 0}
            footnote={
              serving &&
              `${serving.degraded} degraded · ${serving.down} down${
                serving.idle > 0 ? ` · ${serving.idle} idle` : ""
              }`
            }
            tone={
              serving && serving.down > 0
                ? "danger"
                : serving && serving.degraded > 0
                  ? "warn"
                  : "default"
            }
            to="/endpoints"
            isLoading={endpoints.isLoading || deployments.isLoading}
            error={endpoints.error ?? deployments.error}
          />
          <StatCard
            label="Workloads"
            value={workloads.data?.length ?? 0}
            footnote={
              footprint &&
              `${footprint.ready} replicas across ${footprint.clusters} ${
                footprint.clusters === 1 ? "cluster" : "clusters"
              }`
            }
            to="/workloads"
            isLoading={workloads.isLoading}
            error={workloads.error}
          />
          <StatCard
            label="GPU allocated"
            value={`${allocation?.percent ?? 0}%`}
            footnote={
              allocation &&
              `${allocation.allocated} / ${allocation.total} devices`
            }
            tone={
              allocation && allocation.percent >= FULL_PERCENT
                ? "danger"
                : allocation && allocation.percent >= TIGHT_PERCENT
                  ? "warn"
                  : "default"
            }
            to="/devices"
            isLoading={devices.isLoading}
            error={devices.error}
          />
          <StatCard
            label="Unschedulable"
            value={pending ?? 0}
            footnote="replicas pending > 10m"
            tone={pending ? "warn" : "default"}
            to="/deployments"
            isLoading={deployments.isLoading}
            error={deployments.error}
          />
        </div>

        <NeedsAttention
          items={attention}
          isLoading={attentionLoading}
          error={clusters.error ?? deployments.error ?? endpoints.error}
        />

        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <div className="flex min-w-0 flex-col gap-4">
            <CapacityPressure
              rows={capacity}
              isLoading={devices.isLoading}
              error={devices.error}
            />
            <ServingTraffic window={window} />
          </div>

          <div className="flex min-w-0 flex-col gap-4">
            <ClusterReachability
              rows={reachability}
              isLoading={clusters.isLoading}
              error={clusters.error}
            />
            <RecentOperations
              operations={operations.items}
              isLoading={operations.isLoading}
              error={operations.error}
            />
          </div>
        </div>

        <p className="flex items-center gap-2 text-2xs text-muted-foreground">
          <IconEye className="size-3.5 shrink-0" />
          Read-only console.{" "}
          {stale.length === 0
            ? "Every cluster is reporting, so the counts above are current."
            : `Counts may exclude ${stale
                .map(clusterLabel)
                .join(", ")} — last reconcile is older than the page.`}
        </p>
      </div>
    </div>
  )
}
