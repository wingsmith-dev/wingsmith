import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  asStatBlock,
  formatStat,
  metricLabel,
} from "@/modules/benchmarks/display"

const PERCENTILES = [
  "avg",
  "p50",
  "p90",
  "p95",
  "p99",
  "min",
  "max",
  "std",
] as const

/**
 * Renders a harness's result without claiming to know its schema.
 *
 * Anything shaped like a statistic block — `{unit, avg, p50, …}` — becomes a row
 * in the percentile table, because that is the shape every load generator
 * converges on and it is the one worth reading at a glance. Scalars become a
 * short list beside it. Anything else is shown as the JSON it is, which is the
 * honest rendering of a field whose shape nothing promised and the reason this
 * view never needs changing when a harness adds a metric.
 */
export function BenchmarkSummaryView({
  summary,
}: {
  summary: Record<string, unknown>
}) {
  const stats: [string, ReturnType<typeof asStatBlock>][] = []
  const scalars: [string, string | number | boolean][] = []
  const other: [string, unknown][] = []

  for (const [key, value] of Object.entries(summary)) {
    const block = asStatBlock(value)
    if (block) {
      stats.push([key, block])
    } else if (
      typeof value === "number" ||
      typeof value === "string" ||
      typeof value === "boolean"
    ) {
      scalars.push([key, value])
    } else {
      other.push([key, value])
    }
  }

  return (
    <div className="space-y-4">
      {stats.length > 0 && (
        <div className="overflow-x-auto rounded-lg border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="whitespace-nowrap">Metric</TableHead>
                <TableHead className="whitespace-nowrap">Unit</TableHead>
                {PERCENTILES.map((p) => (
                  <TableHead key={p} className="text-right whitespace-nowrap">
                    {p}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {stats.map(([key, block]) => (
                <TableRow key={key}>
                  <TableCell className="font-mono text-2xs whitespace-nowrap">
                    {metricLabel(key)}
                  </TableCell>
                  <TableCell className="font-mono text-2xs text-muted-foreground">
                    {block?.unit ?? "—"}
                  </TableCell>
                  {PERCENTILES.map((p) => (
                    <TableCell
                      key={p}
                      className="text-right font-mono text-2xs whitespace-nowrap"
                    >
                      {formatStat(block?.[p])}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {scalars.length > 0 && (
        <div className="flex flex-wrap gap-x-6 gap-y-1.5">
          {scalars.map(([key, value]) => (
            <span key={key} className="font-mono text-2xs">
              <span className="text-muted-foreground">{metricLabel(key)} </span>
              {String(value)}
            </span>
          ))}
        </div>
      )}

      {other.length > 0 && (
        <pre className="overflow-x-auto rounded-lg border bg-muted/30 p-3 font-mono text-2xs">
          {JSON.stringify(Object.fromEntries(other), null, 2)}
        </pre>
      )}
    </div>
  )
}
