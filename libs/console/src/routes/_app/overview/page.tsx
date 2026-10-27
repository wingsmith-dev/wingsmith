import { PageHeader } from "@/components/layout/page-header"
import { StatTile } from "@/components/layout/stat-tile"
import { anyTruncated } from "@/lib/paginate"
import { deriveFleetAttention } from "@/modules/overview/attention"
import {
  allocationFromSummary,
  capacityFromSummary,
  freshnessNote,
  reachabilityRows,
} from "@/modules/overview/fleet"
import {
  useFleetSummary,
  useOverviewClusters,
  useOverviewDeployments,
  useOverviewDevices,
  useOverviewEndpoints,
  useOverviewNodes,
  useRecentOperations,
  useUndecidedDiscovered,
} from "@/modules/overview/queries"
import { useReadOnly } from "@/modules/settings/store"
import { IconEye } from "@tabler/icons-react"
import { CapacityPressure } from "./CapacityPressure"
import { ClusterReachability } from "./ClusterReachability"
import { NeedsAttention } from "./NeedsAttention"
import { RecentOperations } from "./RecentOperations"

/** Above this share of the fleet allocated, a placement is a coin toss. */
const TIGHT_PERCENT = 75
const FULL_PERCENT = 90

/**
 * The fleet page, ordered as the questions are asked: what is broken, what is
 * tight, what changed. The tiles and capacity panel come from the plane's fleet
 * summary, which counts every row; the attention list needs the rows themselves
 * and drains the collections. The operations feed is the one recent slice.
 */
export function OverviewPage() {
  const summary = useFleetSummary()
  const clusters = useOverviewClusters()
  const nodes = useOverviewNodes()
  const devices = useOverviewDevices()
  const deployments = useOverviewDeployments()
  const endpoints = useOverviewEndpoints()
  const discovered = useUndecidedDiscovered()
  const operations = useRecentOperations()

  const fleet = summary.data
  const serving = fleet?.endpoints
  const allocation = fleet && allocationFromSummary(fleet.devices)
  const pending = fleet?.deployments.unschedulable_replicas
  const capacity = fleet && capacityFromSummary(fleet.devices)
  const reachability = clusters.data && reachabilityRows(clusters.data)
  const readOnly = useReadOnly()
  const freshness = freshnessNote(clusters.data, Boolean(clusters.error))
  const partial = anyTruncated(
    clusters,
    nodes,
    devices,
    deployments,
    endpoints,
    discovered
  )

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

  const attentionSources = [
    clusters,
    nodes,
    devices,
    deployments,
    endpoints,
    discovered,
  ]

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
      />

      <div className="flex flex-col gap-5 p-4">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile
            size="lg"
            label="Endpoints serving"
            value={serving?.serving ?? 0}
            footnote={
              serving
                ? `${serving.degraded} degraded · ${serving.down} down${
                    serving.idle > 0 ? ` · ${serving.idle} idle` : ""
                  }`
                : null
            }
            tone={
              serving && serving.down > 0
                ? "danger"
                : serving && serving.degraded > 0
                  ? "warn"
                  : "default"
            }
            to="/endpoints"
            isLoading={summary.isLoading}
            error={summary.error}
          />
          <StatTile
            size="lg"
            label="Workloads"
            value={fleet?.workloads.total ?? 0}
            footnote={
              fleet
                ? `${fleet.deployments.replicas_ready} replicas across ${
                    fleet.deployments.clusters_serving
                  } ${
                    fleet.deployments.clusters_serving === 1
                      ? "cluster"
                      : "clusters"
                  }`
                : null
            }
            to="/workloads"
            isLoading={summary.isLoading}
            error={summary.error}
          />
          <StatTile
            size="lg"
            label="GPU allocated"
            value={`${allocation?.percent ?? 0}%`}
            footnote={
              allocation
                ? `${allocation.allocated} / ${allocation.total} devices`
                : null
            }
            tone={
              allocation && allocation.percent >= FULL_PERCENT
                ? "danger"
                : allocation && allocation.percent >= TIGHT_PERCENT
                  ? "warn"
                  : "default"
            }
            to="/devices"
            isLoading={summary.isLoading}
            error={summary.error}
          />
          <StatTile
            size="lg"
            label="Unschedulable"
            value={pending ?? 0}
            footnote="replicas pending > 10m"
            tone={pending ? "warn" : "default"}
            to="/deployments"
            isLoading={summary.isLoading}
            error={summary.error}
          />
        </div>

        <NeedsAttention
          items={attention}
          isLoading={attentionLoading}
          error={clusters.error ?? deployments.error ?? endpoints.error}
          clusterCount={clusters.data?.length}
          incomplete={attentionSources.some((query) => query.error)}
        />

        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <div className="flex min-w-0 flex-col gap-4">
            <CapacityPressure
              rows={capacity}
              isLoading={summary.isLoading}
              error={summary.error}
            />
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

        {partial && (
          <p
            role="status"
            className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-warning"
          >
            The attention list may be incomplete: a collection was larger than
            the console reads in one pass.
          </p>
        )}

        {(readOnly || freshness) && (
          <p className="flex items-center gap-2 text-2xs text-muted-foreground">
            {readOnly && <IconEye className="size-3.5 shrink-0" />}
            {[readOnly && "Read-only console.", freshness]
              .filter(Boolean)
              .join(" ")}
          </p>
        )}
      </div>
    </div>
  )
}
