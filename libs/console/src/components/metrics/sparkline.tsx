import { cn } from "@/lib/utils"

import type { MetricSeriesPoint } from "@/modules/metrics/types"

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
