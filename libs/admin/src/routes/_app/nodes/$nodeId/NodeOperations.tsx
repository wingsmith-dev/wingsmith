import { DataTable } from "@/components/layout/data-table"
import { operationColumns } from "@/components/machinery/operation-columns"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { Button } from "@/components/ui/button"
import { useOperations } from "@/modules/machinery/queries"
import type { SupplyNode } from "@/modules/supply/types"

/**
 * Draining is not a flag on the node — it is an Operation whose target is the
 * node, and the cordon is the state it leaves behind. Both live here so the two
 * are never mistaken for one field.
 */
export function NodeOperations({ node }: { node: SupplyNode }) {
  const list = useOperations({ target: node.id })

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h2 className="font-mono text-2xs uppercase tracking-[0.18em] text-muted-foreground/50">
          Operations on this node
        </h2>
        <div className="flex gap-2">
          <NewOperationDialog
            defaultType="node.drain"
            defaultTarget={node.id}
            trigger={
              <Button size="sm" variant="outline">
                Drain
              </Button>
            }
          />
          <NewOperationDialog
            defaultType={node.cordoned ? "node.uncordon" : "node.cordon"}
            defaultTarget={node.id}
            trigger={
              <Button size="sm" variant="outline">
                {node.cordoned ? "Uncordon" : "Cordon"}
              </Button>
            }
          />
        </div>
      </div>
      <DataTable
        columns={operationColumns()}
        list={list}
        rowKey={(operation) => operation.id}
        empty="Nothing has been asked of this node."
      />
    </div>
  )
}
