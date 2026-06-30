import {
  ChartLegend,
  TimeChart,
  type ChartSeries,
} from "@/components/metrics/time-chart"
import { cn } from "@/lib/utils"
import { sumCurvePoints } from "@/modules/metrics/rollup"
import type { MetricSeries, MetricSeriesPoint } from "@/modules/metrics/types"

/**
 * The series colours, by the job the colour does.
 *
 * `CATEGORICAL` is identity — two things that are not more or less of each
 * other, so two hues. `STEP` is one hue light to dark, because p50, p95 and p99
 * are the same measurement at increasing severity and a reader should see that
 * order in the colour rather than have to recall which hue was which.
 *
 * Both are `var()` handles onto the palette in `index.css`, where the light and
 * dark steps live together and where the note on validating them is.
 */
const CATEGORICAL = ["var(--viz-series-1)", "var(--viz-series-2)"] as const
const STEP = [
  "var(--viz-step-1)",
  "var(--viz-step-2)",
  "var(--viz-step-3)",
] as const

/**
 * One metric's curve.
 *
 * Rates are summed across whatever curves came back; a quantile is not. Adding
 * two p99s produces a number that was nobody's latency, and averaging them is no
 * better — so a quantile takes the first curve and a rate takes the total. At
 * the endpoint scope the gateway answers with one curve either way, which makes
 * this a guard against a future breakdown rather than a live difference.
 */
function rate(series: MetricSeries[], metric: string): MetricSeriesPoint[] {
  return sumCurvePoints(series, metric)
}

function quantile(series: MetricSeries[], metric: string): MetricSeriesPoint[] {
  return series.find((curve) => curve.metric === metric)?.points ?? []
}

function Panel({
  title,
  note,
  series,
  unit,
  className,
}: {
  title: string
  note?: string
  series: ChartSeries[]
  unit: string
  className?: string
}) {
  return (
    <section className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="font-mono text-2xs tracking-wider text-muted-foreground/70 uppercase">
          {title}
        </h3>
        <ChartLegend series={series} />
      </div>
      <TimeChart series={series} unit={unit} />
      {note ? (
        <p className="text-2xs text-muted-foreground/60">{note}</p>
      ) : null}
    </section>
  )
}

/**
 * What a tenant's traffic looked like over the chosen window.
 *
 * Three charts and not one: requests, tokens and latency are three different
 * units, and putting any two of them on one pair of axes would need a second
 * y-scale — which makes the crossings of the two lines look like events when
 * they are an artefact of how the scales were chosen.
 */
export function ServingCharts({ curves }: { curves: MetricSeries[] }) {
  const traffic: ChartSeries[] = [
    {
      key: "rps",
      label: "Requests",
      color: CATEGORICAL[0],
      points: rate(curves, "serving.requests_per_second"),
    },
  ]

  const tokens: ChartSeries[] = [
    {
      key: "out",
      label: "Output",
      color: CATEGORICAL[0],
      points: rate(curves, "serving.output_tokens_per_second"),
    },
    {
      key: "in",
      label: "Input",
      color: CATEGORICAL[1],
      points: rate(curves, "serving.input_tokens_per_second"),
    },
  ]

  const latency: ChartSeries[] = [
    {
      key: "p50",
      label: "p50",
      color: STEP[0],
      points: quantile(curves, "serving.latency_p50"),
    },
    {
      key: "p95",
      label: "p95",
      color: STEP[1],
      points: quantile(curves, "serving.latency_p95"),
    },
    {
      key: "p99",
      label: "p99",
      color: STEP[2],
      points: quantile(curves, "serving.latency_p99"),
    },
  ]

  return (
    <div className="grid gap-6 border-t p-5 lg:grid-cols-2">
      <Panel title="Requests / sec" series={traffic} unit="per_second" />
      <Panel title="Tokens / sec" series={tokens} unit="per_second" />
      <Panel
        title="Latency"
        className="lg:col-span-2"
        series={latency}
        unit="seconds"
        note="End to end, as the front door measured it — your network to us is not included."
      />
    </div>
  )
}
