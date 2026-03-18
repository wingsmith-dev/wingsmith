import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { formatRelative } from "@/lib/format"
import { useProjectOptions } from "@/modules/binding/queries"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { allocationPhaseTone } from "@/modules/machinery/display"
import { useAllocations } from "@/modules/machinery/queries"
import type { Allocation } from "@/modules/machinery/types"
import { useNavigate, useSearch } from "@tanstack/react-router"

const LIVE = [
  { value: "true", label: "Live" },
  { value: "false", label: "Released" },
]

const columns: Column<Allocation>[] = [
  {
    key: "id",
    header: "Allocation",
    cell: (allocation) => (
      <ResourceCell kind="allocation">
        <span className="block font-mono text-xs">
          {allocation.workload}
          <Id value={allocation.id} className="ml-2" />
        </span>
      </ResourceCell>
    ),
  },
  {
    key: "cluster",
    header: "Cluster",
    cell: (allocation) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {allocation.cluster_name}
      </span>
    ),
  },
  {
    key: "project",
    header: "Project",
    cell: (allocation) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {allocation.project_slug}
      </span>
    ),
  },
  {
    key: "class",
    header: "Class",
    cell: (allocation) => (
      <Badge variant="secondary" className="font-mono text-2xs">
        {allocation.device_class}
      </Badge>
    ),
  },
  {
    key: "devices",
    header: "Devices live",
    align: "right",
    cell: (allocation) => (
      <span className="font-mono text-xs">
        {allocation.devices.filter((d) => d.live).length}/{allocation.device_count}
      </span>
    ),
  },
  {
    key: "gang",
    header: "Gang",
    cell: (allocation) =>
      allocation.gang_complete ? (
        <span className="font-mono text-2xs text-emerald-600 dark:text-emerald-400">
          complete
        </span>
      ) : (
        <span className="font-mono text-2xs text-amber-600 dark:text-amber-400">
          partial
        </span>
      ),
  },
  {
    key: "phase",
    header: "Phase",
    cell: (allocation) => (
      <StatusLabel tone={allocationPhaseTone(allocation.phase)}>
        {allocation.phase}
      </StatusLabel>
    ),
  },
  {
    key: "over_quota",
    header: "Over quota",
    secondary: true,
    cell: (allocation) =>
      allocation.over_quota ? (
        <Badge
          variant="outline"
          className="border-amber-500/40 bg-amber-500/10 font-mono text-2xs text-amber-600 dark:text-amber-400"
        >
          over quota
        </Badge>
      ) : (
        <span className="font-mono text-2xs text-muted-foreground/40">—</span>
      ),
  },
  {
    key: "acquired",
    header: "Acquired",
    align: "right",
    cell: (allocation) => (
      <span className="font-mono text-2xs text-muted-foreground/70">
        {formatRelative(allocation.acquired_at)}
      </span>
    ),
  },
]

/**
 * Released allocations are rows, not absences: the allocation is how a finished
 * commitment stays auditable, and `live` says which side of that line each one
 * is on without anybody comparing timestamps.
 */
export function AllocationsPage() {
  const search = useSearch({ from: "/_app/allocations" })
  const navigate = useNavigate({ from: "/allocations" })
  const { data: clusters } = useClusterOptions()
  const { data: projects } = useProjectOptions()
  const list = useAllocations({
    cluster: search.cluster,
    project: search.project,
    device: search.device,
    live: search.live === undefined ? undefined : search.live === "true",
  })

  const set = (key: "cluster" | "project" | "live") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Allocations"
        description="observed · devices committed together to one workload"
      />

      <div className="space-y-4 p-4">
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
            label="Project"
            value={search.project ?? ALL}
            onChange={set("project")}
            options={(projects ?? []).map((project) => ({
              value: project.id,
              label: project.slug,
            }))}
          />
          <FilterSelect
            label="State"
            value={search.live ?? ALL}
            onChange={set("live")}
            options={LIVE}
          />
          {search.device && (
            <span className="font-mono text-2xs text-muted-foreground/60">
              device {search.device}
            </span>
          )}
        </div>

        <DataTable
          columns={columns}
          list={list}
          rowKey={(allocation) => allocation.id}
          empty="Nothing holds capacity right now."
        />
      </div>
    </div>
  )
}
