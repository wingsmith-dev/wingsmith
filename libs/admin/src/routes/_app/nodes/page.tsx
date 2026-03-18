import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { nodeColumns } from "@/components/supply/columns"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { NODE_CONDITIONS } from "@/modules/supply/display"
import { useNodes } from "@/modules/supply/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { GroupedNodes } from "./GroupedNodes"

const CORDONED = [
  { value: "true", label: "Cordoned" },
  { value: "false", label: "Schedulable" },
]

export function NodesPage() {
  const search = useSearch({ from: "/_app/nodes" })
  const navigate = useNavigate({ from: "/nodes" })
  const { data: clusters } = useClusterOptions()

  const list = useNodes({
    cluster: search.cluster,
    pool: search.pool,
    condition: search.condition,
    cordoned: search.cordoned === undefined ? undefined : search.cordoned === "true",
  })

  const set = (key: "cluster" | "condition" | "cordoned") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Nodes"
        description="observed · membership, conditions, cordon state"
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
            label="Condition"
            value={search.condition ?? ALL}
            onChange={set("condition")}
            options={NODE_CONDITIONS.map((c) => ({ value: c, label: c }))}
          />
          <FilterSelect
            label="Cordon"
            value={search.cordoned ?? ALL}
            onChange={set("cordoned")}
            options={CORDONED}
          />
        </div>

        <GroupedNodes
          columns={nodeColumns({ showCluster: false })}
          list={list}
          empty="No nodes match these filters."
        />
      </div>
    </div>
  )
}
