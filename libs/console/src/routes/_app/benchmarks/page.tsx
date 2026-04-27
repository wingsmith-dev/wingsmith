import { benchmarkColumns } from "@/components/benchmarks/benchmark-columns"
import { NewBenchmarkRequestDialog } from "@/components/benchmarks/new-request"
import { DataTable } from "@/components/layout/data-table"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { shortenId } from "@/lib/format"
import { BENCHMARK_PHASES } from "@/modules/benchmarks/display"
import { useBenchmarks } from "@/modules/benchmarks/queries"
import { getRouteApi, Link } from "@tanstack/react-router"
import { useState } from "react"

const route = getRouteApi("/_app/benchmarks")

/**
 * Every run in the fleet.
 *
 * Runs rather than requests, because there is no collection of requests on the
 * wire — a request is reached through the run it produced, or through the link
 * the creation dialog navigates to. `?request=` narrows this same table to one
 * ask's children, which is the query the CLI makes too.
 *
 * The only write on this page is the request. A run is observed and carries no
 * action of any kind.
 */
export function BenchmarksPage() {
  const search = route.useSearch()
  const navigate = route.useNavigate()
  const [creating, setCreating] = useState(false)
  const benchmarks = useBenchmarks({
    deployment: search.deployment,
    cluster: search.cluster,
    harness: search.harness,
    phase: search.phase,
    request: search.request,
  })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Benchmarks"
        description="requests are declared · runs and their verdicts are observed"
        actions={<Button onClick={() => setCreating(true)}>New request</Button>}
        readActions={
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
            <SelectTrigger className="w-40 font-mono text-xs">
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
      <div className="space-y-4 p-4">
        {search.request && (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-muted-foreground">
              Showing the runs of one request.
            </span>
            <Badge variant="secondary" className="font-mono text-2xs" asChild>
              <Link
                to="/benchmarks/requests/$requestId"
                params={{ requestId: search.request }}
              >
                {shortenId(search.request)}
              </Link>
            </Badge>
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                navigate({ search: (s) => ({ ...s, request: undefined }) })
              }
            >
              Clear
            </Button>
          </div>
        )}

        <DataTable
          columns={benchmarkColumns({
            showDeployment: true,
            showRequest: true,
          })}
          list={benchmarks}
          rowKey={(run) => run.id}
          empty="Nothing has been measured yet."
        />
      </div>

      {creating && (
        <NewBenchmarkRequestDialog
          open
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
    </div>
  )
}
