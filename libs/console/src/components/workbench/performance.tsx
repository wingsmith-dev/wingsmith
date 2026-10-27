import { BandTable } from "@/components/benchmarks/band-table"
import { LoadCurvePanel } from "@/components/benchmarks/curve"
import { StatRow, StatTile } from "@/components/layout/stat-tile"
import {} from "@/components/benchmarks/summary-view"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { asStatBlock, kneeTile } from "@/modules/benchmarks/display"
import {
  useBenchmarkCurve,
  useRequestBands,
} from "@/modules/benchmarks/queries"
import type { BenchmarkBand, BenchmarkRun } from "@/modules/benchmarks/types"

/**
 * The suite view for a load measurement: the workload page's Workbench tab,
 * against one request instead of one workload.
 *
 * It is the same three things in the same order because it answers the same
 * question: the tiles say where the configuration saturates, the curve shows
 * how it got there, and the bands say whether each input length met its target.
 * The difference is the scope. On a workload the curve is "the most recent ramp
 * anybody ran"; here it is this ask and nothing else, which is what makes two
 * suites comparable to each other.
 *
 * Nothing on this page computes a knee. The curve, the knee and the band
 * verdicts are all the plane's read against the request's pinned criteria: a
 * console that walked the steps itself would be a second, quieter answer to
 * where a configuration bends.
 */
export function PerformanceSuite({
  requestId,
  runs,
  open,
  stepped,
}: {
  requestId: string
  runs: BenchmarkRun[] | undefined
  open: boolean
  /** Whether the request walks a knob. A single run has one point and no curve. */
  stepped: boolean
}) {
  const curve = useBenchmarkCurve(requestId)
  const { data: bands } = useRequestBands(requestId, open)
  const knee = kneeTile(curve.data)

  const peak = peakThroughput(runs)
  const graded = (runs ?? []).filter((run) => !run.discard).length

  return (
    <div className="space-y-4">
      <StatRow>
        <StatTile
          label={
            curve.data ? `Knee · ${curve.data.step_field}` : "Knee concurrency"
          }
          value={knee.value}
          size={knee.small ? "sm" : "md"}
        />
        <StatTile
          label="Peak throughput"
          value={
            peak === null ? "—" : `${Math.round(peak).toLocaleString()} tok/s`
          }
          size={peak === null ? "sm" : "md"}
        />
        <StatTile label="Graded runs" value={String(graded)} />
        <StatTile
          label="Warm-ups"
          value={String((runs ?? []).filter((run) => run.discard).length)}
          size="sm"
        />
        <StatTile label="Steps" value={String(runs?.length ?? 0)} />
      </StatRow>

      {/*
        Only a stepped request has a curve. A single run is one point, and
        drawing an axis through it would suggest a shape that one measurement
        cannot have.

        `placed` is true and the ramp button is inert here: a suite is one ask
        that has already been made, and the way to take another is a new request
        instead of a second ramp grafted onto this one's record.
      */}
      {stepped ? (
        <LoadCurvePanel
          curve={{ request: requestId, ...curve }}
          placed
          onStartRamp={() => {}}
        />
      ) : (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">One load point</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              This request walks no knob, so it measured a single point and has
              no curve. Where a configuration saturates is a question a ramp
              answers; the run below says what it did at this one load.
            </p>
          </CardContent>
        </Card>
      )}

      <div>
        <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
          Per input-length band
        </h2>
        <BandTable bands={(bands?.items ?? []) as BenchmarkBand[]} />
      </div>
    </div>
  )
}

/**
 * The best output rate any graded step reached.
 *
 * Read off the harness's own summary, so a run whose tool emitted the figure
 * under another name contributes nothing instead of a wrong number.
 */
function peakThroughput(runs: BenchmarkRun[] | undefined): number | null {
  let best: number | null = null
  for (const run of runs ?? []) {
    if (run.discard || !run.summary) continue
    const block = asStatBlock(run.summary.output_token_throughput)
    const value = block?.avg ?? block?.p50
    if (value === undefined) continue
    if (best === null || value > best) best = value
  }
  return best
}
