import { DataTable, type Column } from "@/components/layout/data-table"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { ResourceCell } from "@/components/layout/resource-cell"
import { formatError } from "@/lib/format"
import {
  LIMIT_SCOPES,
  LIMIT_SCOPE_LABEL,
  commitsNothing,
  formatObjective,
  formatWindow,
} from "@/modules/binding/display"
import {
  useDeleteServingQuota,
  useProjectOptions,
  useServingEndpointOptions,
  useServingQuotas,
} from "@/modules/binding/queries"
import type { LimitScope, ServingQuota } from "@/modules/binding/types"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"
import {
  Committed,
  EnforcementBadge,
  OnExceedBadge,
  ScopeBadge,
  ShadowNotice,
  StandardBadge,
} from "./ServingLimits"
import { ServingQuotaForm } from "./ServingQuotaForm"
import { ServingQuotaSheet } from "./ServingQuotaSheet"

function columns(
  onEdit: (quota: ServingQuota) => void,
  onDelete: (quota: ServingQuota) => void
): Column<ServingQuota>[] {
  return [
    {
      key: "endpoint",
      header: "Endpoint",
      cell: (quota) => (
        <ResourceCell kind="endpoint">
          <div className="font-mono text-xs">{quota.endpoint_name}</div>
          {commitsNothing(quota) && (
            <div className="font-mono text-2xs text-warning">
              commits nothing
            </div>
          )}
        </ResourceCell>
      ),
    },
    {
      key: "enforcement",
      header: "Enforcement",
      cell: (quota) => <EnforcementBadge enforced={quota.enforced} />,
    },
    {
      key: "rpm",
      header: "Requests/min",
      align: "right",
      cell: (quota) => (
        <div className="space-y-0.5">
          <Committed value={quota.committed_rpm} />
          {quota.burst_rpm !== null && (
            <div className="font-mono text-2xs text-muted-foreground">
              burst {quota.burst_rpm}
            </div>
          )}
        </div>
      ),
    },
    {
      key: "tpm",
      header: "Tokens/min",
      align: "right",
      cell: (quota) => <Committed value={quota.committed_tpm} />,
    },
    {
      key: "concurrency",
      header: "Concurrent",
      align: "right",
      cell: (quota) => <Committed value={quota.max_concurrent_requests} />,
    },
    {
      key: "max_output",
      header: "Max output",
      align: "right",
      secondary: true,
      cell: (quota) => <Committed value={quota.max_output_tokens} />,
    },
    {
      key: "scope",
      header: "Scope",
      cell: (quota) => <ScopeBadge scope={quota.limit_scope} />,
    },
    {
      key: "on_exceed",
      header: "Over the limit",
      secondary: true,
      cell: (quota) => (
        <OnExceedBadge
          onExceed={quota.on_exceed}
          queueTimeoutMs={quota.queue_timeout_ms}
        />
      ),
    },
    {
      key: "objectives",
      header: "Objective",
      align: "right",
      secondary: true,
      cell: (quota) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {quota.success_rate_objective === null ? (
            <Committed value={null} />
          ) : (
            `${formatObjective(quota.success_rate_objective)} · ${formatWindow(
              quota.objective_window_seconds
            )}`
          )}
        </span>
      ),
    },
    {
      key: "standard",
      header: "Standard",
      cell: (quota) => <StandardBadge standard={quota.standard} />,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (quota) => (
        <RowActions
          actions={[
            { label: "Edit", onSelect: () => onEdit(quota) },
            {
              label: "Delete",
              destructive: true,
              onSelect: () => onDelete(quota),
            },
          ]}
        />
      ),
    },
  ]
}

/**
 * Traffic, not devices. The gateway checks these on every request, in front of
 * the clusters, and measures the outcome where the plane never sees it — so
 * this table is declaration only and carries no usage column at all.
 */
export function ServingQuotas({
  creating,
  onCreatingChange,
}: {
  creating: boolean
  onCreatingChange: (creating: boolean) => void
}) {
  const search = useSearch({ from: "/_app/quotas" })
  const navigate = useNavigate({ from: "/quotas" })
  const { data: endpoints } = useServingEndpointOptions()
  const { data: projects } = useProjectOptions()

  const list = useServingQuotas({
    endpoint: search.endpoint,
    project: search.project,
    enforced:
      search.enforcement === undefined
        ? undefined
        : search.enforcement === "enforced",
    scope: search.scope,
  })

  const remove = useDeleteServingQuota()
  const [editing, setEditing] = useState<ServingQuota | undefined>()
  const [doomed, setDoomed] = useState<ServingQuota | undefined>()
  const [viewing, setViewing] = useState<ServingQuota | undefined>()

  const set =
    (key: "endpoint" | "project" | "enforcement" | "scope") =>
    (value: string) =>
      navigate({
        search: (prev) => ({
          ...prev,
          [key]: value === ALL ? undefined : value,
        }),
      })

  const loaded = list.items ?? []
  const shadowed = loaded.filter((quota) => !quota.enforced).length

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        Requests and tokens, checked by the gateway on every request. An
        endpoint with no row here is not rate limited at all.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <FilterSelect
          label="Endpoint"
          value={search.endpoint ?? ALL}
          onChange={set("endpoint")}
          options={(endpoints ?? []).map((endpoint) => ({
            value: endpoint.id,
            label: endpoint.name,
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
          label="Enforcement"
          value={search.enforcement ?? ALL}
          onChange={set("enforcement")}
          options={[
            { value: "enforced", label: "enforced" },
            { value: "shadow", label: "shadow mode" },
          ]}
        />
        <FilterSelect
          label="Scope"
          value={search.scope ?? ALL}
          onChange={set("scope")}
          options={LIMIT_SCOPES.map((scope: LimitScope) => ({
            value: scope,
            label: LIMIT_SCOPE_LABEL[scope],
          }))}
        />
      </div>

      <ShadowNotice shadowed={shadowed} total={loaded.length} />

      <DataTable
        columns={columns(setEditing, setDoomed)}
        list={list}
        rowKey={(quota) => quota.id}
        onRowClick={setViewing}
        empty="No serving quota declared. Endpoints without one are not rate limited."
      />

      {creating && (
        <ServingQuotaForm
          open
          onOpenChange={(open) => !open && onCreatingChange(false)}
        />
      )}
      {editing && (
        <ServingQuotaForm
          key={editing.revision}
          open
          quota={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      {viewing && (
        <ServingQuotaSheet
          key={viewing.revision}
          quota={viewing}
          onOpenChange={(open) => !open && setViewing(undefined)}
        />
      )}
      <DeleteDialog
        icon="quota"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="serving quota"
        name={doomed?.endpoint_name ?? ""}
        note="The gateway stops limiting that endpoint entirely."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Serving quota deleted", {
                description: doomed.endpoint_name,
              })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete serving quota")),
          })
        }
      />
    </div>
  )
}
