import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { useProjectOptions } from "@/modules/binding/queries"
import { Button } from "@/components/ui/button"
import { formatError, shortenId } from "@/lib/format"
import { useDeleteEndpoint, useEndpoints } from "@/modules/demand/queries"
import type { Endpoint } from "@/modules/demand/types"
import { EndpointForm } from "./EndpointForm"
import { useState } from "react"
import { toast } from "sonner"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

function columns(
  onEdit: (endpoint: Endpoint) => void,
  onDelete: (endpoint: Endpoint) => void
): Column<Endpoint>[] {
  return [
    {
      key: "name",
      header: "Endpoint",
      cell: (endpoint) => (
        <ResourceCell kind="endpoint">
          <Link
            to="/endpoints/$endpointId"
            params={{ endpointId: endpoint.id }}
            className="block"
          >
            <span className="font-mono text-xs">{endpoint.name}</span>
            <Id value={shortenId(endpoint.id)} className="ml-2" />
          </Link>
        </ResourceCell>
      ),
    },
    {
      key: "display_name",
      header: "Display name",
      cell: (endpoint) => (
        <span className="text-xs text-muted-foreground">
          <Dash>{endpoint.display_name}</Dash>
        </span>
      ),
    },
    {
      key: "visibility",
      header: "Visibility",
      cell: (endpoint) => (
        <Badge variant="secondary" className="font-mono text-2xs">
          {endpoint.visibility}
        </Badge>
      ),
    },
    {
      key: "routing",
      header: "Routing",
      cell: (endpoint) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {endpoint.routing_strategy}
        </span>
      ),
    },
    {
      key: "targets",
      header: "Targets",
      align: "right",
      cell: (endpoint) => (
        <span className="font-mono text-xs">
          {endpoint.targets.filter((t) => t.enabled).length}/
          {endpoint.targets.length}
        </span>
      ),
    },
    {
      key: "enabled",
      header: "Enabled",
      align: "right",
      cell: (endpoint) => (
        <span className="font-mono text-2xs">
          {endpoint.enabled ? "yes" : "no"}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (endpoint) => (
        <RowActions
          actions={[
            { label: "Edit", onSelect: () => onEdit(endpoint) },
            {
              label: "Delete",
              destructive: true,
              onSelect: () => onDelete(endpoint),
            },
          ]}
        />
      ),
    },
  ]
}

/**
 * An endpoint has no cluster at any depth. It spans clusters by construction —
 * that is what the weighted target set is for — so there is nothing here to
 * scope by cluster and no column pretending otherwise.
 */
export function EndpointsPage() {
  const search = useSearch({ from: "/_app/endpoints" })
  const navigate = useNavigate({ from: "/endpoints" })
  const { data: projects } = useProjectOptions()
  const list = useEndpoints({ project: search.project })
  const remove = useDeleteEndpoint()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Endpoint | undefined>()
  const [doomed, setDoomed] = useState<Endpoint | undefined>()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Endpoints"
        description="declared · the stable public name clients call"
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            New endpoint
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Project"
            value={search.project ?? ALL}
            onChange={(value) =>
              navigate({
                search: (prev) => ({
                  ...prev,
                  project: value === ALL ? undefined : value,
                }),
              })
            }
            options={(projects ?? []).map((project) => ({
              value: project.id,
              label: project.slug,
            }))}
          />
        </div>

        <DataTable
          columns={columns(setEditing, setDoomed)}
          list={list}
          rowKey={(endpoint) => endpoint.id}
          empty="No endpoints published."
        />
      </div>

      {creating && (
        <EndpointForm
          open
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
      {editing && (
        <EndpointForm
          key={editing.revision}
          open
          endpoint={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="endpoint"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="endpoint"
        name={doomed?.name ?? ""}
        note="Clients calling this name start failing immediately."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Endpoint deleted", { description: doomed.name })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete endpoint")),
          })
        }
      />
    </div>
  )
}
