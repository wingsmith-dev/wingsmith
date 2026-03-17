import { NodesTable as SupplyNodesTable } from "@/components/supply/nodes-table"
import { useClusterNodes } from "@/modules/clusters/queries"

export function NodesTable({ clusterId }: { clusterId: string }) {
  const { data, isLoading, error } = useClusterNodes(clusterId)
  return (
    <SupplyNodesTable
      nodes={data?.items}
      isLoading={isLoading}
      error={error}
      empty="No nodes have joined this cluster yet."
    />
  )
}
