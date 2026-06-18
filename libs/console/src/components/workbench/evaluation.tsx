import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { StatRow, StatTile } from "@/components/workbench/stats"
import { useBenchmark, useRequestRuns } from "@/modules/benchmarks/queries"
import { formatMs } from "@/modules/benchmarks/display"
import { Link } from "@tanstack/react-router"

/**
 * What one evalscope run actually measured. Reads `summary.reports` — the raw
 * per-dataset JSON EvalScope writes — and renders the one number per report the
 * dataset is judged on: score, sample count, and the wall-clock it cost.
 *
 * No Claimed column and no Drift column. Those are verdict-shaped statements
 * about a baseline this console has nowhere to pull from yet, and rendering a
 * number beside a number it is not being compared against is how a table lies.
 * The artifact baseline table exists as `demand_artifact_baseline`; when it is
 * queried here the columns come back. Until then, absence beats invention.
 */

interface EvalReport {
  dataset_name?: string
  dataset_pretty_name?: string
  metrics?: Array<{
    identity?: { name?: string }
    name?: string
    num?: number
    score?: number
    macro_score?: number
  }>
  perf_metrics?: {
    summary?: {
      latency?: { mean?: number }
      throughput?: { avg_output_tps?: number }
    }
  }
  execution_summary?: { requested?: number; succeeded?: number; errored?: number }
}

interface EvalSummary {
  reports?: EvalReport[]
}

export function EvaluationRun({ id }: { id: string }) {
  const { data: run, isLoading, error } = useBenchmark(id)

  if (isLoading)
    return (
      <div className="rounded-lg border bg-card p-6 text-center text-xs text-muted-foreground">
        Loading…
      </div>
    )
  if (error || !run)
    return (
      <div className="rounded-lg border bg-card p-6 text-center text-xs text-red-600">
        Could not load the run.
      </div>
    )

  const summary = (run.summary as EvalSummary | null) ?? null
  const reports = summary?.reports ?? []
  const named = reports.filter((r) => r.dataset_name)
  const totalSamples = named.reduce(
    (acc, r) => acc + (r.metrics?.[0]?.num ?? 0),
    0
  )
  const totalLatencyMs = named.reduce(
    (acc, r) => acc + (r.perf_metrics?.summary?.latency?.mean ?? 0) * (r.metrics?.[0]?.num ?? 0),
    0
  )

  return (
    <div className="space-y-4">
      <StatRow>
        <StatTile label="Datasets" value={String(named.length)} />
        <StatTile label="Samples" value={totalSamples.toLocaleString()} />
        <StatTile label="Wall clock" value={formatMs(totalLatencyMs)} small />
        <StatTile
          label="Completed"
          value={run.finished_at ? new Date(run.finished_at).toLocaleString() : "—"}
          small
        />
      </StatRow>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Datasets</CardTitle>
          <p className="mt-1 text-2xs text-muted-foreground/60">
            One row per dataset the run sat. Scores read straight from the
            harness's reports; the plane does not re-grade them.
          </p>
        </CardHeader>
        <CardContent className="px-0">
          <div className="divide-y divide-border/60 border-y">
            <div className="flex items-center gap-2 px-3 py-1.5 font-mono text-2xs tracking-[0.12em] text-muted-foreground/40 uppercase">
              <span className="w-40 shrink-0">Dataset</span>
              <span className="min-w-0 flex-1">Metric</span>
              <span className="w-20 shrink-0 text-right">Samples</span>
              <span className="w-20 shrink-0 text-right">Score</span>
            </div>
            {named.length === 0 ? (
              <div className="px-3 py-6 text-center text-2xs text-muted-foreground">
                No report — the harness did not run or wrote nothing under
                /benchmark-artifacts/reports.
              </div>
            ) : (
              named.map((r, idx) => {
                const metric = r.metrics?.[0]
                const label =
                  metric?.identity?.name ??
                  metric?.name ??
                  "accuracy"
                const score = metric?.score
                const num = metric?.num ?? 0
                return (
                  <div
                    key={`${r.dataset_name ?? "unknown"}-${idx}`}
                    className="flex items-center gap-2 px-3 py-1.5 hover:bg-muted/40"
                  >
                    <span className="w-40 shrink-0 truncate font-mono text-xs">
                      {r.dataset_pretty_name ?? r.dataset_name}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-2xs text-muted-foreground">
                      {label}
                    </span>
                    <span className="w-20 shrink-0 text-right font-mono text-2xs text-muted-foreground/60 tabular-nums">
                      {num.toLocaleString()}
                    </span>
                    <span className="w-20 shrink-0 text-right font-mono text-xs tabular-nums">
                      {typeof score === "number" ? score.toFixed(4) : "—"}
                    </span>
                  </div>
                )
              })
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

/** The request view — one row per run it expanded into. */
export function EvaluationSuite({ id }: { id: string }) {
  const { items, isLoading } = useRequestRuns(id)
  const list = items ?? []

  if (isLoading)
    return (
      <div className="rounded-lg border bg-card p-6 text-center text-xs text-muted-foreground">
        Loading runs…
      </div>
    )

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Runs</CardTitle>
        <p className="mt-1 text-2xs text-muted-foreground/60">
          One line per run. The score is the run's own reports; requests that
          sit several datasets land as several rows.
        </p>
      </CardHeader>
      <CardContent className="px-0">
        <div className="divide-y divide-border/60 border-y">
          <div className="flex items-center gap-2 px-3 py-1.5 font-mono text-2xs tracking-[0.12em] text-muted-foreground/40 uppercase">
            <span className="w-32 shrink-0">Run</span>
            <span className="min-w-0 flex-1">Datasets</span>
            <span className="w-20 shrink-0 text-right">Samples</span>
            <span className="w-20 shrink-0 text-right">Score</span>
            <span className="w-24 shrink-0 text-right">Phase</span>
          </div>
          {list.length === 0 ? (
            <div className="px-3 py-6 text-center text-2xs text-muted-foreground">
              No runs on this request.
            </div>
          ) : (
            list.map((run) => {
              const summary = (run.summary as EvalSummary | null) ?? null
              const reports = (summary?.reports ?? []).filter((r) => r.dataset_name)
              const samples = reports.reduce(
                (acc, r) => acc + (r.metrics?.[0]?.num ?? 0),
                0
              )
              const scores = reports
                .map((r) => r.metrics?.[0]?.score)
                .filter((v): v is number => typeof v === "number")
              const headline =
                scores.length === 1
                  ? scores[0].toFixed(4)
                  : scores.length > 1
                    ? `${Math.min(...scores).toFixed(3)}–${Math.max(...scores).toFixed(3)}`
                    : "—"
              return (
                <div
                  key={run.id}
                  className="flex items-center gap-2 px-3 py-1.5 hover:bg-muted/40"
                >
                  <Link
                    to="/workbench/$runId"
                    params={{ runId: run.id }}
                    className="w-32 shrink-0 truncate font-mono text-xs text-blue-600 hover:underline dark:text-blue-400"
                  >
                    {run.id.slice(-10)}
                  </Link>
                  <span className="min-w-0 flex-1 truncate text-2xs text-muted-foreground">
                    {reports.length > 0
                      ? reports.map((r) => r.dataset_name).join(", ")
                      : "—"}
                  </span>
                  <span className="w-20 shrink-0 text-right font-mono text-2xs text-muted-foreground/60 tabular-nums">
                    {samples > 0 ? samples.toLocaleString() : "—"}
                  </span>
                  <span className="w-20 shrink-0 text-right font-mono text-xs tabular-nums">
                    {headline}
                  </span>
                  <span className="w-24 shrink-0 text-right font-mono text-2xs text-muted-foreground">
                    {run.phase}
                  </span>
                </div>
              )
            })
          )}
        </div>
      </CardContent>
    </Card>
  )
}
