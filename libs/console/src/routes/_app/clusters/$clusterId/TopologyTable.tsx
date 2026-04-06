import { DataTable } from "@/components/layout/data-table"
import { topologyColumns } from "@/components/supply/columns"
import { useTopologyDomains } from "@/modules/supply/queries"

export function ClusterTopologyTable({ clusterId }: { clusterId: string }) {
  const list = useTopologyDomains({ cluster: clusterId })
  return (
    <DataTable
      columns={topologyColumns()}
      list={list}
      rowKey={(domain) => domain.id}
      empty="No topology domains reported."
    />
  )
}
