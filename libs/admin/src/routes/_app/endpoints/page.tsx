import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { useProjectOptions } from "@/modules/binding/queries"
import { useEndpoints } from "@/modules/demand/queries"
import type { Endpoint } from "@/modules/demand/types"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

const columns: Column<Endpoint>[] = [
  {
    key: "name",
    header: "Endpoint",
    cell: (endpoint) => (
      <Link
        to="/endpoints/$endpointId"
        params={{ endpointId: endpoint.id }}
        className="block"
      >
        <span className="font-mono text-xs">{endpoint.name}</span>
        <Id value={endpoint.id} className="ml-2" />
      </Link>
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
        {endpoint.targets.filter((t) => t.enabled).length}/{endpoint.targets.length}
      </span>
    ),
  },
  {
    key: "enabled",
    header: "Enabled",
    align: "right",
    cell: (endpoint) => (
      <span className="font-mono text-2xs">{endpoint.enabled ? "yes" : "no"}</span>
    ),
  },
]

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

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Endpoints"
        description="declared · the stable public name clients call"
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
          columns={columns}
          list={list}
          rowKey={(endpoint) => endpoint.id}
          empty="No endpoints published."
        />
      </div>
    </div>
  )
}
