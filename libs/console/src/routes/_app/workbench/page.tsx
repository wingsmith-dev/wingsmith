import { NewRunDialog } from "@/components/benchmarks/new-run"
import { runColumns } from "@/components/benchmarks/run-columns"
import { DataTable } from "@/components/layout/data-table"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { shortenId } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { BENCHMARK_PHASES } from "@/modules/benchmarks/display"
import {
  RUN_KINDS,
  RUN_KIND_ORDER,
  runKind,
  type RunKind,
} from "@/modules/benchmarks/kinds"
import { mockRuns } from "@/modules/benchmarks/mock"
import { useBenchmarks, useHarnesses } from "@/modules/benchmarks/queries"
import { getRouteApi, Link } from "@tanstack/react-router"
import { useMemo, useState } from "react"

const route = getRouteApi("/_app/workbench")

/**
 * Every run in the fleet, whatever question it was asking.
 *
 * One list rather than one per kind, because the runs are one object — a
 * harness, a target, parameters, a result — and four pages differing only in a
 * filter would be four places to look for "did it finish". The kind is the tab
 * strip and the first column instead.
 *
 * Runs rather than requests, because there is no collection of requests on the
 * wire — a request is reached through the runs it produced. `?request=` narrows
 * this same table to one ask's children.
 */
export function WorkbenchPage() {
  const search = route.useSearch()
  const navigate = route.useNavigate()
  const [creating, setCreating] = useState(false)

  const { data: catalog } = useHarnesses()
  const { data: clusters } = useClusterOptions()

  /**
   * A kind is a group of harnesses and the plane filters by one harness name,
   * so a kind narrows server-side only while it has a single registered
   * harness behind it — which is true of all three today.
   *
   * TODO: the plane should accept `kind` (or a repeated `harness`) on
   * `GET /v1/benchmarks`. Until it does, a kind that grows a second harness
   * silently stops narrowing, and filtering the loaded page client-side instead
   * would drop rows that are simply on the next cursor.
   */
  const harnessFilter = useMemo(() => {
    if (!search.type) return undefined
    const names = (catalog?.items ?? [])
      .map((entry) => entry.name)
      .filter((name) => runKind(name) === search.type)
    return names.length === 1 ? names[0] : undefined
  }, [search.type, catalog])

  const benchmarks = useBenchmarks({
    deployment: search.deployment,
    cluster: search.cluster,
    harness: harnessFilter,
    phase: search.phase,
    request: search.request,
  })

  /**
   * The invented rows sit in front of the real ones rather than being merged
   * into the page: the list is cursor-paginated, and splicing fake rows into a
   * page of real ones would put them in an order the cursor does not know
   * about. They also never appear while a request is being narrowed, because a
   * mock run is not a child of anybody's ask.
   */
  const mocked = useMemo(() => {
    if (!search.mock || search.request) return []
    return mockRuns().filter(
      (run) => !search.type || runKind(run.harness) === search.type
    )
  }, [search.mock, search.request, search.type])

  const list = useMemo(
    () =>
      mocked.length
        ? { ...benchmarks, items: [...mocked, ...(benchmarks.items ?? [])] }
        : benchmarks,
    [benchmarks, mocked]
  )

  const set = (key: "cluster" | "phase") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  const view = search.type ?? ALL

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Workbench"
        description="what has been asked of a serving surface, and what it answered"
        actions={<Button onClick={() => setCreating(true)}>New run</Button>}
        readActions={
          <div className="flex items-center gap-2">
            <Label
              htmlFor="workbench-mock"
              className="font-mono text-2xs tracking-[0.12em] text-muted-foreground uppercase"
            >
              Mock
            </Label>
            <Switch
              id="workbench-mock"
              checked={!!search.mock}
              onCheckedChange={(next) =>
                navigate({
                  search: (prev) => ({ ...prev, mock: next || undefined }),
                })
              }
            />
          </div>
        }
      />

      <div className="space-y-4 p-4">
        <Tabs
          value={view}
          onValueChange={(next) =>
            navigate({
              search: (prev) => ({
                ...prev,
                type: next === ALL ? undefined : (next as RunKind),
              }),
            })
          }
        >
          {/*
            The kind is a tab and not a filter select because it is the axis
            somebody arrives on — "is the conformance run green" — and because
            each one is a different question with a different answer column, not
            a narrowing of one list. The selects beside the table narrow within
            whichever question is open.
          */}
          <TabsList>
            <TabsTrigger value={ALL}>All</TabsTrigger>
            {RUN_KIND_ORDER.map((kind) => (
              <TabsTrigger
                key={kind}
                value={kind}
                title={RUN_KINDS[kind].question}
              >
                {RUN_KINDS[kind].label}
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value={view} className="space-y-4">
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
                label="Status"
                value={search.phase ?? ALL}
                onChange={set("phase")}
                options={BENCHMARK_PHASES.map((phase) => ({
                  value: phase,
                  label: phase,
                }))}
              />
            </div>

            {search.request && (
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="text-muted-foreground">
                  Showing the runs of one request.
                </span>
                <Badge
                  variant="secondary"
                  className="font-mono text-2xs"
                  asChild
                >
                  <Link
                    to="/workbench/requests/$requestId"
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

            {search.mock && mocked.length > 0 && (
              <p className="rounded-lg border border-dashed border-amber-500/40 bg-amber-500/5 px-3 py-2 text-2xs text-warning">
                <span className="font-semibold tracking-[0.12em] uppercase">
                  Mock ·{" "}
                </span>
                The first {mocked.length} row
                {mocked.length === 1 ? " is" : "s are"} invented, so the views
                for the kinds with no harness behind them can be opened. Turn
                the switch off to see only what the plane holds.
              </p>
            )}

            <DataTable
              columns={runColumns()}
              list={list}
              rowKey={(run) => run.id}
              empty={
                search.type
                  ? `No ${RUN_KINDS[search.type].label.toLowerCase()} run has been taken yet.`
                  : "Nothing has been asked of anything yet."
              }
            />

            {search.type && (
              <p className="px-1 text-2xs text-muted-foreground">
                {RUN_KINDS[search.type].question}
              </p>
            )}
          </TabsContent>
        </Tabs>
      </div>

      {creating && (
        <NewRunDialog
          open
          kind={search.type}
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
    </div>
  )
}
