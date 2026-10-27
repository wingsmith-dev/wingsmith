import { Id } from "@/components/layout/mono"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { shortenId } from "@/lib/format"
import { bandLabel, formatMs, orderBands } from "@/modules/benchmarks/display"
import type { BenchmarkBand } from "@/modules/benchmarks/types"
import { Link } from "@tanstack/react-router"

/**
 * One request's runs, read back through the input-length band each landed in.
 *
 * There is no total row. The standard sets a separate
 * time-to-first-token target for each band, so a single aggregate over a whole
 * sweep evidences none of them: a request that averaged four seconds may have
 * been comfortably inside the target at 2K tokens and far outside it at 100K,
 * and the average says neither. This table exists to stop that number being
 * quoted, so it does not print one.
 *
 * The three columns are not three views of the same thing: `p90` and `p50` are
 * the worst of the band, because percentiles do not average and a claim about a
 * band has to hold at every load point inside it, while `avg` is weighted by
 * request count, which is the one of the three that genuinely combines.
 *
 * A band nothing landed in is absent, not empty. Absent means the sweep
 * did not cover it, which is not the same as meeting its target.
 */
export function BandTable({ bands }: { bands: BenchmarkBand[] }) {
  if (bands.length === 0) {
    return (
      <div className="rounded-lg border bg-card p-6 text-center text-xs text-muted-foreground">
        No band has reported yet. Warm-up runs are excluded outright: they were
        never a measurement.
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto rounded-lg border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="whitespace-nowrap">Input band</TableHead>
              <TableHead className="text-right whitespace-nowrap">
                Runs
              </TableHead>
              <TableHead className="text-right whitespace-nowrap">
                Requests
              </TableHead>
              <TableHead className="text-right whitespace-nowrap">
                TTFT p90 (worst)
              </TableHead>
              <TableHead className="text-right whitespace-nowrap">
                TTFT p50 (worst)
              </TableHead>
              <TableHead className="text-right whitespace-nowrap">
                TTFT avg (weighted)
              </TableHead>
              <TableHead className="whitespace-nowrap">Incomplete</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {orderBands(bands).map((band) => (
              <TableRow key={band.band}>
                <TableCell className="whitespace-nowrap">
                  <span className="font-mono text-xs">
                    {bandLabel(band.band)}
                  </span>
                  <Id value={band.band} className="ml-2" />
                </TableCell>
                <TableCell className="text-right font-mono text-2xs">
                  {band.runs}
                </TableCell>
                <TableCell className="text-right font-mono text-2xs">
                  {band.requests.toLocaleString()}
                </TableCell>
                <TableCell className="text-right font-mono text-2xs">
                  {formatMs(band.ttft_p90_ms)}
                </TableCell>
                <TableCell className="text-right font-mono text-2xs">
                  {formatMs(band.ttft_p50_ms)}
                </TableCell>
                <TableCell className="text-right font-mono text-2xs">
                  {formatMs(band.ttft_avg_ms)}
                </TableCell>
                <TableCell>
                  {band.incomplete.length === 0 ? (
                    <span className="font-mono text-2xs text-muted-foreground">
                      —
                    </span>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {band.incomplete.map((run) => (
                        <Badge
                          key={run}
                          variant="outline"
                          className="border-amber-500/40 font-mono text-2xs text-warning"
                          asChild
                        >
                          <Link to="/workbench/$runId" params={{ runId: run }}>
                            {shortenId(run)}
                          </Link>
                        </Badge>
                      ))}
                    </div>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <p className="font-mono text-2xs text-muted-foreground">
        One target per band, so no row totals them. A band absent from this
        table was not covered by the sweep, which is not the same as meeting its
        target; a run under Incomplete reported no time-to-first-token at all.
      </p>
    </div>
  )
}
