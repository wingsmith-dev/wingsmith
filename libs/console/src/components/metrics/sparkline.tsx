import { cn } from "@/lib/utils"

import { formatMetric } from "@/modules/metrics/format"
import type { MetricSeriesPoint, MetricUnit } from "@/modules/metrics/types"

/**
 * A trend, drawn with SVG rather than a charting library.
 *
 * The console has no chart dependency and this does not justify adding one: a
 * sparkline is a path and a fill, and the panels it serves want shape — is this
 * flat, climbing, or spiky — rather than a readable axis. A real chart with
 * legends and tooltips is a different component and can bring its own library
 * when something needs it.
 */
export function Sparkline({
  points,
  className,
  tone = "primary",
  size = "sm",
}: {
  points: MetricSeriesPoint[]
  className?: string
  tone?: "primary" | "warn" | "danger"
  /** `lg` fills under the line and is tall enough to read a shape off. */
  size?: "sm" | "lg"
}) {
  if (points.length < 2) return null

  const values = points.map((p) => p.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const width = 100
  const height = size === "lg" ? 56 : 28

  const coords = points.map((point, index) => {
    const x = (index / (points.length - 1)) * width
    const y = height - ((point.value - min) / span) * (height - 2) - 1
    return `${x.toFixed(2)},${y.toFixed(2)}`
  })

  const stroke = {
    primary: "stroke-primary",
    warn: "stroke-amber-500",
    danger: "stroke-destructive",
  }[tone]

  const fill = {
    primary: "fill-primary",
    warn: "fill-amber-500",
    danger: "fill-destructive",
  }[tone]

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={cn(size === "lg" ? "h-24 w-full" : "h-7 w-full", className)}
      aria-hidden
    >
      {size === "lg" && (
        <polygon
          points={`0,${height} ${coords.join(" ")} ${width},${height}`}
          className={cn(fill, "opacity-10")}
        />
      )}
      <polyline
        points={coords.join(" ")}
        fill="none"
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
        className={cn(stroke, "opacity-80")}
      />
    </svg>
  )
}

/** The tones a curve can be drawn in. */
export type CurveTone = "primary" | "warn" | "danger"

const STROKE: Record<CurveTone, string> = {
  primary: "stroke-primary",
  warn: "stroke-amber-500",
  danger: "stroke-destructive",
}

const FILL: Record<CurveTone, string> = {
  primary: "fill-primary",
  warn: "fill-amber-500",
  danger: "fill-destructive",
}

export interface Curve {
  points: MetricSeriesPoint[]
  tone: CurveTone
}

/**
 * Several curves on one scale, with that scale written down beside them.
 *
 * Two things a bare `Sparkline` cannot do, and the page needs both.
 *
 * It states the axis. A sparkline is normalized to its own extremes, so a
 * metric wobbling between 4.01 and 4.02 draws the same picture as one swinging
 * from zero to a thousand — the right trade for a tile the size of a word, and
 * the wrong one for the page's main chart, where the shape means nothing until
 * somebody can read what it is a shape of. The floor is labelled rather than
 * forced to zero, because pinning a latency band to zero flattens every change
 * worth seeing; stating it is what stops the autoscale being a trick.
 *
 * And it puts curves on a *shared* scale. Three percentiles each normalized to
 * their own extremes are three unrelated pictures — p50 and p99 drawn to
 * different rulers cross and diverge for reasons that are pure arithmetic. One
 * scale is the only way a legend saying p50, p95, p99 means anything.
 *
 * `x` is the timestamp and not the index, so curves the store answered at
 * different resolutions still line up in time.
 */
export function MetricChart({
  curves,
  unit,
  spanLabel,
  fill = false,
}: {
  curves: Curve[]
  unit: MetricUnit
  /** How far back the curves reach, said under the left edge. */
  spanLabel: string
  /** Fill under the curve. For a single series only; it hides the others. */
  fill?: boolean
}) {
  const drawn = curves.filter((curve) => curve.points.length >= 2)
  if (drawn.length === 0) return null

  const all = drawn.flatMap((curve) => curve.points)
  const values = all.map((point) => point.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const from = Math.min(...all.map((point) => point.at))
  const to = Math.max(...all.map((point) => point.at))
  const elapsed = to - from || 1

  const width = 100
  const height = 56
  const plot = (points: MetricSeriesPoint[]) =>
    points
      .map((point) => {
        const x = ((point.at - from) / elapsed) * width
        const y = height - ((point.value - min) / span) * (height - 2) - 1
        return `${x.toFixed(2)},${y.toFixed(2)}`
      })
      .join(" ")

  return (
    <div className="space-y-1.5">
      <div className="flex gap-2">
        <div className="flex h-24 w-16 shrink-0 flex-col justify-between text-right font-mono text-2xs text-muted-foreground tabular-nums">
          <span className="-translate-y-[3px]">{formatMetric(max, unit)}</span>
          <span className="translate-y-[3px]">{formatMetric(min, unit)}</span>
        </div>
        <div className="relative min-w-0 flex-1">
          <div className="pointer-events-none absolute inset-x-0 top-0 border-t border-dashed border-foreground/15" />
          <div className="pointer-events-none absolute inset-x-0 bottom-0 border-t border-dashed border-foreground/15" />
          <svg
            viewBox={`0 0 ${width} ${height}`}
            preserveAspectRatio="none"
            className="h-24 w-full"
            aria-hidden
          >
            {fill && drawn.length === 1 && (
              <polygon
                points={`0,${height} ${plot(drawn[0].points)} ${width},${height}`}
                className={cn(FILL[drawn[0].tone], "opacity-10")}
              />
            )}
            {drawn.map((curve, index) => (
              <polyline
                key={index}
                points={plot(curve.points)}
                fill="none"
                strokeWidth={1.5}
                vectorEffect="non-scaling-stroke"
                className={cn(STROKE[curve.tone], "opacity-80")}
              />
            ))}
          </svg>
        </div>
      </div>
      <div className="flex justify-between pl-[72px] font-mono text-2xs text-muted-foreground">
        <span>{spanLabel} ago</span>
        <span>now</span>
      </div>
    </div>
  )
}
