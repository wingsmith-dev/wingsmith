import { DataTable } from "@/components/layout/data-table"
import { operationColumns } from "@/components/machinery/operation-columns"
import { useOperations } from "@/modules/machinery/queries"
import type { SupplyNode } from "@/modules/supply/types"

/**
 * History, and only history — starting an operation lives in the header, where
 * the jobs to be done belong. Draining is not a flag on the node either way: it
 * is an Operation whose target is the node, and the cordon is the state it
 * leaves behind.
 */
export function NodeOperations({ node }: { node: SupplyNode }) {
  const list = useOperations({ target: node.id })

  return (
    <div>
      <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
        Operation history
      </h2>
      <DataTable
        columns={operationColumns()}
        list={list}
        rowKey={(operation) => operation.id}
        empty="Nothing has been asked of this node."
      />
    </div>
  )
}
