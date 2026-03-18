import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { useProjectOptions } from "@/modules/binding/queries"
import { useQuotas } from "@/modules/binding/queries"
import type { Quota } from "@/modules/binding/types"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"

const columns: Column<Quota>[] = [
  {
    key: "project",
    header: "Project",
    cell: (quota) => (
      <span className="font-mono text-xs">{quota.project_slug}</span>
    ),
  },
  {
    key: "scope",
    header: "Scope",
    cell: (quota) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {quota.cluster_name ?? "every cluster"}
      </span>
    ),
  },
  {
    key: "class",
    header: "Class",
    cell: (quota) => (
      <Badge variant="secondary" className="font-mono text-2xs">
        {quota.device_class}
      </Badge>
    ),
  },
  {
    key: "guaranteed",
    header: "Guaranteed",
    align: "right",
    cell: (quota) => (
      <span className="font-mono text-xs">{quota.guaranteed_devices}</span>
    ),
  },
  {
    key: "limit",
    header: "Limit",
    align: "right",
    cell: (quota) => (
      <span className="font-mono text-xs">
        {quota.limit_devices ?? <span className="text-muted-foreground/40">none</span>}
      </span>
    ),
  },
  {
    key: "used",
    header: "Allocated",
    align: "right",
    cell: (quota) => (
      <span className="font-mono text-xs">{quota.usage.allocated_devices}</span>
    ),
  },
  {
    key: "usage",
    header: "Against limit",
    cell: (quota) => {
      const ceiling = quota.limit_devices ?? quota.guaranteed_devices
      const pct = ceiling === 0 ? 0 : (quota.usage.allocated_devices / ceiling) * 100
      return (
        <div className="min-w-28 space-y-1">
          <Progress value={Math.min(pct, 100)} className="h-1.5" />
          <span className="font-mono text-2xs text-muted-foreground/60">
            {quota.usage.live_allocations} live ·{" "}
            {quota.usage.over_quota_devices} over quota
          </span>
        </div>
      )
    },
  },
  {
    key: "headroom",
    header: "Headroom",
    align: "right",
    cell: (quota) => (
      <span
        className={
          quota.over_limit
            ? "font-mono text-xs text-destructive"
            : "font-mono text-xs"
        }
      >
        {quota.headroom_devices ?? "∞"}
      </span>
    ),
  },
  {
    key: "over_quota",
    header: "Over-quota",
    align: "right",
    secondary: true,
    cell: (quota) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {quota.over_quota_enabled ? `weight ${quota.over_quota_weight}` : "disabled"}
      </span>
    ),
  },
]

/**
 * A guaranteed floor and a fair share, with usage beside them and never merged
 * into one number. `over_limit` and `headroom_devices` are the plane's own
 * derivations, so this page never computes a second opinion.
 */
export function QuotasPage() {
  const search = useSearch({ from: "/_app/quotas" })
  const navigate = useNavigate({ from: "/quotas" })
  const { data: projects } = useProjectOptions()
  const { data: clusters } = useClusterOptions()
  const list = useQuotas({
    project: search.project,
    cluster: search.cluster,
    class: search.class,
  })

  const set = (key: "project" | "cluster") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Quotas"
        description="declared floor and fair share · observed usage beside it"
      />

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
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
            label="Cluster"
            value={search.cluster ?? ALL}
            onChange={set("cluster")}
            options={(clusters ?? []).map((cluster) => ({
              value: cluster.id,
              label: clusterLabel(cluster),
            }))}
          />
        </div>

        <DataTable
          columns={columns}
          list={list}
          rowKey={(quota) => quota.id}
          empty="No quotas declared."
        />
      </div>
    </div>
  )
}
