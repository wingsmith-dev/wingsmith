import { DataTable } from "@/components/layout/data-table"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { workloadColumns } from "@/components/discovered/workload-columns"
import { Input } from "@/components/ui/input"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { useDiscoveredWorkloads } from "@/modules/discovered/queries"
import { DISCOVERY_STATUSES } from "@/modules/discovered/types"
import { useNavigate, useSearch } from "@tanstack/react-router"

/**
 * The import surface. Every cluster Wingsmith adopts was already running something, and
 * until someone decides what to do with each of those workloads they are real
 * GPUs held by objects the plane does not know about. A sighting is observed and
 * the decision beside it is the operator's, so nothing on this page changes a cluster
 * except promotion.
 */
export function DiscoveredPage() {
  const search = useSearch({ from: "/_app/discovered" })
  const navigate = useNavigate({ from: "/discovered" })
  const { data: clusters } = useClusterOptions()
  const list = useDiscoveredWorkloads({
    cluster: search.cluster,
    namespace: search.namespace,
    status: search.status,
    search: search.search,
  })

  const set = (key: "cluster" | "status") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Discovered"
        description="observed · workloads running on the clusters that Wingsmith did not create"
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
            label="Status"
            value={search.status ?? ALL}
            onChange={set("status")}
            options={DISCOVERY_STATUSES.map((status) => ({
              value: status,
              label: status,
            }))}
          />
          <Input
            value={search.search ?? ""}
            onChange={(e) =>
              navigate({
                search: (prev) => ({
                  ...prev,
                  search: e.target.value || undefined,
                }),
              })
            }
            placeholder="name, namespace or model"
            className="h-8 w-56 font-mono text-xs"
          />
        </div>

        <DataTable
          columns={workloadColumns()}
          list={list}
          rowKey={(workload) => workload.id}
          empty="The clusters report nothing running that Wingsmith did not declare."
        />
      </div>
    </div>
  )
}
