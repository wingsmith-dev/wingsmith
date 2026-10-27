import { StatTile } from "@/components/layout/stat-tile"
import { TopologyBadge } from "@/components/demand/serving-topology"
import { DataTable, type Column } from "@/components/layout/data-table"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import {} from "@/components/ui/card"
import { formatError, formatRelative, shortenId } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { footprintOfServing, footprintSummary } from "@/modules/demand/deploy"
import {
  WORKLOAD_KINDS,
  WORKLOAD_PHASES,
  workloadOperationalStatus,
} from "@/modules/demand/display"
import { useDeleteWorkload, useWorkloads } from "@/modules/demand/queries"
import { scaleUnitFor } from "@/modules/demand/topology"
import type { Workload } from "@/modules/demand/types"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"
import { WorkloadForm } from "./WorkloadForm"

function columns(
  onEdit: (workload: Workload) => void,
  onDelete: (workload: Workload) => void
): Column<Workload>[] {
  return [
    {
      key: "name",
      header: "Workload",
      cell: (workload) => (
        <ResourceCell kind="workload">
          <Link
            to="/workloads/$workloadId"
            params={{ workloadId: workload.id }}
            className="block"
          >
            <span className="font-mono text-xs">{workload.name}</span>
            <Id value={shortenId(workload.id)} className="ml-2" />
            {/* On the name and not in a column of its own: almost every
                workload is aggregated, so a column would be empty down its
                whole length, and the badge has to be beside the thing it is
                true of, and not somewhere a reader has to go looking. */}
            <TopologyBadge workload={workload} />
          </Link>
        </ResourceCell>
      ),
    },
    {
      key: "phase",
      header: "Status",
      cell: (workload) => {
        const status = workloadOperationalStatus(workload)
        return (
          <span title={status.reason ?? undefined}>
            <StatusLabel tone={status.tone}>{status.phase}</StatusLabel>
          </span>
        )
      },
    },
    {
      key: "clusters",
      header: "Deployments",
      cell: (workload) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {workload.deployments.length === 0 ? (
            <Dash>{null}</Dash>
          ) : (
            `${workload.deployments.length} ${
              workload.deployments.length === 1 ? "cluster" : "clusters"
            }`
          )}
        </span>
      ),
    },
    {
      key: "scale",
      header: "Replicas",
      align: "right",
      cell: (workload) => (
        <span className="font-mono text-xs">
          {workload.scale.ready}/{workload.scale.desired}
          <span className="ml-1 text-2xs text-muted-foreground">
            {scaleUnitFor(workload)}
          </span>
        </span>
      ),
    },
    {
      key: "serving",
      header: "Serving / artifact",
      cell: (workload) => (
        <div className="flex flex-col gap-0.5">
          <span className="font-mono text-xs">
            {footprintSummary(footprintOfServing(workload.serving))}
          </span>
          <span className="font-mono text-2xs text-muted-foreground">
            {workload.artifact_tag ?? workload.kind}
          </span>
        </div>
      ),
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      secondary: true,
      cell: (workload) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(workload.updated_at ?? workload.created_at)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (workload) => (
        <RowActions
          actions={[
            { label: "Edit", onSelect: () => onEdit(workload) },
            {
              label: "Delete",
              destructive: true,
              onSelect: () => onDelete(workload),
            },
          ]}
        />
      ),
    },
  ]
}

/**
 * What was asked to run, globally. A workload names no cluster: it declares
 * weights, how to serve them, a kind and a total, and the clusters column shows
 * where placement put it: a list, and sometimes an empty one.
 *
 * The three kinds of thing stay apart here as they do everywhere else.
 * `scale.desired` is declared, `scale.ready` is what the clusters report,
 * `phase` is derived from the deployments, and scaling is an operation on the
 * detail page instead of a number to type in a table.
 */
export function WorkloadsPage() {
  const search = useSearch({ from: "/_app/workloads" })
  const navigate = useNavigate({ from: "/workloads" })
  const { data: clusters } = useClusterOptions()
  const remove = useDeleteWorkload()
  const [editing, setEditing] = useState<Workload | undefined>()
  const [doomed, setDoomed] = useState<Workload | undefined>()
  const list = useWorkloads({
    project: search.project,
    kind: search.kind,
    phase: search.phase,
    artifact: search.artifact,
    cluster: search.cluster,
  })

  const set = (key: "cluster" | "phase" | "kind") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  const workloads = list.items ?? []
  const readyReplicas = workloads.reduce((sum, w) => sum + w.scale.ready, 0)
  const desiredReplicas = workloads.reduce((sum, w) => sum + w.scale.desired, 0)
  const needsAttention = workloads.filter(
    (w) =>
      workloadOperationalStatus(w).needsAttention ||
      w.scale.ready < w.scale.desired
  ).length

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Workloads"
        description="declared · what you asked to run, and every cluster it landed on"
        actions={
          <Button size="lg" asChild>
            <Link to="/workloads/new">New workload</Link>
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
          <StatTile
            size="lg"
            label="Workloads"
            value={list.isLoading ? "—" : list.loaded}
          />
          <StatTile
            size="lg"
            label="Replicas running"
            value={list.isLoading ? "—" : `${readyReplicas}/${desiredReplicas}`}
          />
          <StatTile
            size="lg"
            label="Needs attention"
            value={list.isLoading ? "—" : needsAttention}
            tone={needsAttention > 0 ? "warn" : undefined}
          />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Cluster"
            value={search.cluster ?? ALL}
            onChange={set("cluster")}
            options={(clusters ?? []).map((cluster) => ({
              value: cluster.id,
              label: clusterLabel(cluster),
            }))}
          />
          <FilterSelect
            label="Phase"
            value={search.phase ?? ALL}
            onChange={set("phase")}
            options={WORKLOAD_PHASES.map((p) => ({ value: p, label: p }))}
          />
          <FilterSelect
            label="Kind"
            value={search.kind ?? ALL}
            onChange={set("kind")}
            options={WORKLOAD_KINDS.map((k) => ({ value: k, label: k }))}
          />
        </div>

        <DataTable
          columns={columns(setEditing, setDoomed)}
          list={list}
          rowKey={(workload) => workload.id}
          empty="Nothing has been asked to run."
        />
      </div>

      {editing && (
        <WorkloadForm
          key={editing.revision}
          open
          workload={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="workload"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="workload"
        name={doomed?.name ?? ""}
        note="Every cluster it was expanded onto loses its deployment, and any endpoint target pointing at one stops routing."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Workload deleted", { description: doomed.name })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete workload")),
          })
        }
      />
    </div>
  )
}
