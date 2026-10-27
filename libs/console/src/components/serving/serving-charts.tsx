import {
  ChartLegend,
  TimeChart,
  type ChartSeries,
} from "@/components/metrics/time-chart"
import { cn } from "@/lib/utils"
import { sumCurvePoints } from "@/modules/metrics/rollup"
import type { MetricSeries, MetricSeriesPoint } from "@/modules/metrics/types"

/**
 * The series colors, by the job the color does.
 *
 * `CATEGORICAL` is identity — two things that are not more or less of each
 * other, so two hues. `STEP` is one hue light to dark, because p50, p95 and p99
 * are the same measurement at increasing severity and a reader should see that
 * order in the color instead of having to recall which hue was which.
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

function ChartPanel({
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
        <h3 className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          {title}
        </h3>
        <ChartLegend series={series} />
      </div>
      <TimeChart series={series} unit={unit} />
      {note ? <p className="text-2xs text-muted-foreground">{note}</p> : null}
    </section>
  )
}

/**
 * What a tenant's traffic looked like over the chosen window.
 *
 * One chart per unit, and not one chart: requests, tokens, seconds and a
 * percentage do not share an axis, and putting any two of them on one pair
 * would need a second y-scale — which makes the crossings of the two lines look
 * like events when they are an artifact of how the scales were chosen.
 *
 * Time to first token is split from end-to-end latency for the same reason they
 * are separate rows in an acceptance document: they are quoted against separate
 * ceilings, and a reader checking one against its threshold should not have to
 * pick its line out of the other's.
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

  const ttft: ChartSeries[] = [
    {
      key: "p50",
      label: "p50",
      color: STEP[0],
      points: quantile(curves, "serving.ttft_p50"),
    },
    {
      key: "p95",
      label: "p95",
      color: STEP[1],
      points: quantile(curves, "serving.ttft_p95"),
    },
  ]

  const cache: ChartSeries[] = [
    {
      key: "cached",
      label: "Cached",
      color: CATEGORICAL[0],
      points: quantile(curves, "serving.cache_hit_ratio"),
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
      <ChartPanel title="Requests / sec" series={traffic} unit="per_second" />
      <ChartPanel title="Tokens / sec" series={tokens} unit="per_second" />
      <ChartPanel
        title="Time to first token"
        series={ttft}
        unit="seconds"
        note="Measured at the gateway, so it excludes the network between you and the gateway. A client-side TTFT is this plus the round trip."
      />
      <ChartPanel
        title="Cached input"
        series={cache}
        unit="percent"
        note="Cached input tokens over input tokens, read off the same usage object your client sees as prompt_tokens_details.cached_tokens."
      />
      <ChartPanel
        title="Latency"
        className="lg:col-span-2"
        series={latency}
        unit="seconds"
        note="End to end, as the gateway measured it. The network between you and the gateway is not included."
      />
    </div>
  )
}
