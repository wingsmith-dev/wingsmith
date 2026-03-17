import { PageHeader } from "@/components/layout/page-header"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { NodesTable } from "@/components/supply/nodes-table"
import { useClusters } from "@/modules/clusters/queries"
import { useNodes } from "@/modules/supply/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"

const CONDITIONS = [
  { value: "ready", label: "Ready" },
  { value: "not_ready", label: "Not ready" },
  { value: "unknown", label: "Unknown" },
]

export function NodesPage() {
  const search = useSearch({ from: "/_app/nodes" })
  const navigate = useNavigate({ from: "/nodes" })
  const { data: clusters } = useClusters()
  const { data, isLoading, error } = useNodes({
    cluster_id: search.cluster,
    condition: search.condition,
  })

  const set = (key: "cluster" | "condition") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  const cordoned = data?.items.filter((n) => n.cordoned).length ?? 0

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
            options={(clusters?.items ?? []).map((c) => ({
              value: c.id,
              label: c.display_name,
            }))}
          />
          <FilterSelect
            label="Condition"
            value={search.condition ?? ALL}
            onChange={set("condition")}
            options={CONDITIONS}
          />
          <div className="ml-auto font-mono text-2xs text-muted-foreground/60">
            {data?.count ?? 0} nodes · {cordoned} cordoned
          </div>
        </div>

        <NodesTable
          nodes={data?.items}
          isLoading={isLoading}
          error={error}
          showCluster
          empty="No nodes match these filters."
        />
      </div>
    </div>
  )
}
