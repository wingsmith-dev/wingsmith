import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { Button } from "@/components/ui/button"
import { formatError } from "@/lib/format"
import { useDeleteQuota, useProjectOptions, useQuotas } from "@/modules/binding/queries"
import type { Quota } from "@/modules/binding/types"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { QuotaForm } from "./QuotaForm"
import { useState } from "react"
import { toast } from "sonner"

function columns(
  onEdit: (quota: Quota) => void,
  onDelete: (quota: Quota) => void
): Column<Quota>[] {
  return [
  {
    key: "project",
    header: "Project",
    cell: (quota) => (
      <ResourceCell kind="quota">
        <span className="font-mono text-xs">{quota.project_slug}</span>
      </ResourceCell>
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
  {
    key: "actions",
    header: "",
    align: "right",
    cell: (quota) => (
      <RowActions
        actions={[
          { label: "Edit", onSelect: () => onEdit(quota) },
          { label: "Delete", destructive: true, onSelect: () => onDelete(quota) },
        ]}
      />
    ),
  },
  ]
}

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

  const remove = useDeleteQuota()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Quota | undefined>()
  const [doomed, setDoomed] = useState<Quota | undefined>()

  const set = (key: "project" | "cluster") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Quotas"
        description="declared floor and fair share · observed usage beside it"
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            New quota
          </Button>
        }
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
          columns={columns(setEditing, setDoomed)}
          list={list}
          rowKey={(quota) => quota.id}
          empty="No quotas declared."
        />
      </div>

      {creating && (
        <QuotaForm open onOpenChange={(open) => !open && setCreating(false)} />
      )}
      {editing && (
        <QuotaForm
          key={editing.revision}
          open
          quota={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="quota"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="quota"
        name={`${doomed?.project_slug ?? ""} · ${doomed?.device_class ?? ""}`}
        note="The project keeps no guaranteed floor on that class."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Quota deleted", { description: doomed.project_slug })
            },
            onError: (e) => toast.error(formatError(e, "Could not delete quota")),
          })
        }
      />
    </div>
  )
}
