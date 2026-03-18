import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable } from "@/components/layout/data-table"
import { PageHeader } from "@/components/layout/page-header"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { operationColumns } from "@/components/machinery/operation-columns"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { OPERATION_PHASES } from "@/modules/machinery/display"
import { useOperations } from "@/modules/machinery/queries"
import { OPERATION_TYPES } from "@/modules/machinery/types"
import { useNavigate, useSearch } from "@tanstack/react-router"

/**
 * The imperative surface. Drain, cordon, reset, warm, roll and upgrade all live
 * here rather than as flags hiding on the objects they act upon — an operation
 * has an actor, a reason, attempts and a lifecycle, and a status field has none
 * of those.
 */
export function OperationsPage() {
  const search = useSearch({ from: "/_app/operations" })
  const navigate = useNavigate({ from: "/operations" })
  const { data: clusters } = useClusterOptions()
  const list = useOperations({
    cluster: search.cluster,
    type: search.type,
    phase: search.phase,
    target: search.target,
  })

  const set = (key: "cluster" | "type" | "phase") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Operations"
        description="imperative · a type and a target, with a lifecycle of its own"
        actions={<NewOperationDialog />}
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
            label="Type"
            value={search.type ?? ALL}
            onChange={set("type")}
            options={Object.keys(OPERATION_TYPES).map((t) => ({ value: t, label: t }))}
          />
          <FilterSelect
            label="Phase"
            value={search.phase ?? ALL}
            onChange={set("phase")}
            options={OPERATION_PHASES.map((p) => ({ value: p, label: p }))}
          />
        </div>

        <DataTable
          columns={operationColumns()}
          list={list}
          rowKey={(operation) => operation.id}
          empty="Nothing has been asked of the fleet."
        />
      </div>
    </div>
  )
}
