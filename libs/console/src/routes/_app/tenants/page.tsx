import { DataTable, type Column } from "@/components/layout/data-table"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { formatError, shortenId } from "@/lib/format"
import { useDeleteTenant, useTenants } from "@/modules/binding/queries"
import type { Tenant } from "@/modules/binding/types"
import { Link } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"
import { TenantForm } from "./TenantForm"

function columns(
  onEdit: (tenant: Tenant) => void,
  onDelete: (tenant: Tenant) => void
): Column<Tenant>[] {
  return [
    {
      key: "slug",
      header: "Tenant",
      cell: (tenant) => (
        <ResourceCell kind="tenant">
          <span className="block">
            <Link
              to="/tenants/$tenantId"
              params={{ tenantId: tenant.slug }}
              className="font-mono text-xs hover:underline"
            >
              {tenant.slug}
            </Link>
            <Id value={shortenId(tenant.id)} className="ml-2" />
          </span>
        </ResourceCell>
      ),
    },
    {
      key: "name",
      header: "Name",
      cell: (tenant) => <span className="text-xs">{tenant.name}</span>,
    },
    {
      key: "projects",
      header: "Projects",
      align: "right",
      cell: (tenant) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {tenant.project_count}
        </span>
      ),
    },
    {
      key: "enabled",
      header: "Enabled",
      cell: (tenant) => (
        <StatusLabel tone={tenant.enabled ? "ok" : "danger"}>
          {tenant.enabled ? "yes" : "no"}
        </StatusLabel>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (tenant) => (
        <div className="flex items-center justify-end gap-2">
          <Link
            to="/projects"
            search={{ tenant: tenant.slug }}
            className="font-mono text-2xs text-muted-foreground hover:underline"
          >
            projects →
          </Link>
          <RowActions
            actions={[
              { label: "Edit", onSelect: () => onEdit(tenant) },
              {
                label: "Delete",
                destructive: true,
                onSelect: () => onDelete(tenant),
              },
            ]}
          />
        </div>
      ),
    },
  ]
}

/**
 * The root of the tenancy hierarchy, and the first thing an empty plane needs:
 * a project hangs off a tenant, quota attaches beneath the project, and nothing
 * can be promoted or deployed until one exists.
 */
export function TenantsPage() {
  const list = useTenants()
  const remove = useDeleteTenant()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Tenant | undefined>()
  const [doomed, setDoomed] = useState<Tenant | undefined>()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Tenants"
        description="declared · the billing root a project hangs off"
        actions={<Button onClick={() => setCreating(true)}>New tenant</Button>}
      />
      <div className="space-y-4 p-4">
        <DataTable
          columns={columns(setEditing, setDoomed)}
          list={list}
          rowKey={(tenant) => tenant.id}
          empty="No tenants. A project needs one, so this is where an empty plane starts."
        />
      </div>

      {creating && (
        <TenantForm open onOpenChange={(open) => !open && setCreating(false)} />
      )}
      {editing && (
        <TenantForm
          key={editing.revision}
          open
          tenant={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="tenant"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="tenant"
        name={doomed?.slug ?? ""}
        note="The plane refuses while any project still hangs off it."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Tenant deleted", { description: doomed.slug })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete tenant")),
          })
        }
      />
    </div>
  )
}
