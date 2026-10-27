import { StatTile } from "@/components/layout/stat-tile"
import { DeleteDialog } from "@/components/layout/destructive"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { Button } from "@/components/ui/button"
import {} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {} from "@/lib/utils"
import { formatError } from "@/lib/format"
import { CLUSTER_PHASES, clusterLabel } from "@/modules/clusters/display"
import {
  useClusterRollups,
  useClusters,
  useDeleteCluster,
} from "@/modules/clusters/queries"
import type { Cluster } from "@/modules/clusters/types"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"
import { ClusterCard } from "./ClusterCard"
import { ClusterForm } from "./ClusterForm"

/**
 * Cards, not rows: there are only ever a handful of clusters, and each
 * one carries a declared half and an observed half that a table row flattens.
 * Phase and reachability stay two things: a cluster that is `active` and
 * unreachable is a network problem, a `disabled` one is somebody's decision.
 */
export function ClustersPage() {
  const search = useSearch({ from: "/_app/clusters" })
  const navigate = useNavigate({ from: "/clusters" })
  const list = useClusters({ phase: search.phase, region: search.region })
  const rollups = useClusterRollups()
  const remove = useDeleteCluster()

  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Cluster | null>(null)
  const [deleting, setDeleting] = useState<Cluster | null>(null)

  const clusters = list.items ?? []
  const regions = [...new Set(clusters.map((c) => c.region))].sort()

  const needsAttention = clusters.filter(
    (c) => !c.reachable || c.phase === "draining" || c.sync.state === "stale"
  ).length

  const fleet = (rollups.data ?? []).reduce(
    (acc, r) => ({
      nodesReady: acc.nodesReady + r.nodes_ready,
      nodesTotal: acc.nodesTotal + r.nodes_total,
      gpusAllocated: acc.gpusAllocated + r.devices_allocated,
      gpusTotal: acc.gpusTotal + r.devices_total,
    }),
    { nodesReady: 0, nodesTotal: 0, gpusAllocated: 0, gpusTotal: 0 }
  )

  const set = (key: "phase" | "region") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  const confirmDelete = () => {
    if (!deleting) return
    remove.mutate(
      { ref: deleting.id, revision: deleting.revision },
      {
        onSuccess: () => {
          toast.success("Cluster deleted", { description: deleting.name })
          setDeleting(null)
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not delete cluster")),
      }
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Clusters"
        description="declared enrollment · observed reachability and sync"
        actions={
          <Button onClick={() => setCreating(true)}>Register cluster</Button>
        }
      />

      <div className="space-y-4 p-4">
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
          <StatTile
            size="lg"
            label="Clusters"
            value={String(clusters.length)}
          />
          <StatTile
            size="lg"
            label="Nodes ready"
            value={
              rollups.isLoading
                ? "—"
                : `${fleet.nodesReady}/${fleet.nodesTotal}`
            }
          />
          <StatTile
            size="lg"
            label="GPUs allocated"
            value={
              rollups.isLoading
                ? "—"
                : `${fleet.gpusAllocated}/${fleet.gpusTotal}`
            }
          />
          <StatTile
            size="lg"
            label="Needs attention"
            value={String(needsAttention)}
            tone={needsAttention > 0 ? "warn" : undefined}
          />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Phase"
            value={search.phase ?? ALL}
            onChange={set("phase")}
            options={CLUSTER_PHASES.map((phase) => ({
              value: phase,
              label: phase,
            }))}
          />
          <FilterSelect
            label="Region"
            value={search.region ?? ALL}
            onChange={set("region")}
            options={regions.map((region) => ({
              value: region,
              label: region,
            }))}
          />
        </div>

        {list.isLoading && (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-64 w-full" />
            ))}
          </div>
        )}

        {!!list.error && (
          <p className="py-10 text-center text-sm text-destructive">
            {formatError(list.error, "Failed to load clusters")}
          </p>
        )}

        {!!list.items?.length && (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {list.items.map((cluster) => (
              <ClusterCard
                key={cluster.id}
                cluster={cluster}
                rollup={rollups.byCluster[cluster.id]}
                onEdit={() => setEditing(cluster)}
                onDelete={() => setDeleting(cluster)}
              />
            ))}
          </div>
        )}

        {list.items && list.items.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-14 text-center">
            <p className="text-sm text-muted-foreground">
              No clusters registered yet.
            </p>
            <p className="max-w-sm text-xs text-muted-foreground">
              Registering declares a cluster in the plane; the operator is then
              installed in it and dials home. The cluster page walks you through
              connecting it.
            </p>
            <Button size="sm" onClick={() => setCreating(true)}>
              Register cluster
            </Button>
          </div>
        )}

        {list.hasNextPage && (
          <Button
            variant="outline"
            className="w-full"
            onClick={() => list.fetchNextPage()}
            disabled={list.isFetchingNextPage}
          >
            {list.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        )}
      </div>

      {creating && (
        <ClusterForm
          open
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
      {editing && (
        <ClusterForm
          key={editing.revision}
          cluster={editing}
          open
          onOpenChange={(open) => !open && setEditing(null)}
        />
      )}

      <DeleteDialog
        icon="cluster"
        open={!!deleting}
        onOpenChange={(open) => !open && setDeleting(null)}
        kind="cluster"
        name={deleting ? clusterLabel(deleting) : ""}
        note="Its pools go with it; observed nodes and devices stop being reported."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={confirmDelete}
      />
    </div>
  )
}
