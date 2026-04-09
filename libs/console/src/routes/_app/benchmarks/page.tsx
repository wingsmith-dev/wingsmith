import { DataTable } from "@/components/layout/data-table"
import { PageHeader } from "@/components/layout/page-header"
import { benchmarkColumns } from "@/components/benchmarks/benchmark-columns"
import { BENCHMARK_PHASES } from "@/modules/benchmarks/display"
import { useBenchmarks } from "@/modules/benchmarks/queries"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { getRouteApi } from "@tanstack/react-router"

const route = getRouteApi("/_app/benchmarks")

/**
 * Every run in the fleet. The per-deployment view is this same collection
 * filtered by `?deployment=`, which is also the query the CLI makes — there is
 * no second endpoint hanging off a deployment.
 */
export function BenchmarksPage() {
  const search = route.useSearch()
  const navigate = route.useNavigate()
  const benchmarks = useBenchmarks({
    deployment: search.deployment,
    cluster: search.cluster,
    harness: search.harness,
    phase: search.phase,
  })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Benchmarks"
        description="Measurements taken against deployments, in the harness's own shape."
        actions={
          <Select
            value={search.phase ?? "all"}
            onValueChange={(value) =>
              navigate({
                search: (s) => ({
                  ...s,
                  phase: value === "all" ? undefined : value,
                }),
              })
            }
          >
            <SelectTrigger size="sm" className="w-40 font-mono text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all" className="font-mono text-xs">
                all phases
              </SelectItem>
              {BENCHMARK_PHASES.map((phase) => (
                <SelectItem
                  key={phase}
                  value={phase}
                  className="font-mono text-xs"
                >
                  {phase}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />
      <div className="p-4">
        <DataTable
          columns={benchmarkColumns({ showDeployment: true })}
          list={benchmarks}
          rowKey={(run) => run.id}
          empty="Nothing has been measured yet."
        />
      </div>
    </div>
  )
}
