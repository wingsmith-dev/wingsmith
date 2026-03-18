import { DataTable } from "@/components/layout/data-table"
import { nodeColumns } from "@/components/supply/columns"
import { useNodes } from "@/modules/supply/queries"

export function ClusterNodesTable({ clusterId }: { clusterId: string }) {
  const list = useNodes({ cluster: clusterId })
  return (
    <DataTable
      columns={nodeColumns({ showCluster: false })}
      list={list}
      rowKey={(node) => node.id}
      empty="No nodes have joined this cluster yet."
    />
  )
}
