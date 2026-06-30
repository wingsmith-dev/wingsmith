import { useId, useLayoutEffect, useMemo, useRef, useState } from "react"

import { cn } from "@/lib/utils"
import { formatMetric } from "@/modules/metrics/format"
import type { MetricSeriesPoint, MetricUnit } from "@/modules/metrics/types"

/**
 * A read-off-the-axis time chart, drawn as SVG.
 *
 * `Sparkline` is the other end of the same idea and stays what it is: a shape,
 * no axis, no readout. Its own doc says a chart with axes and a hover layer is a
 * different component that can bring a library when something needs one. This is
 * that component, and it still brings no library — a line chart is a path, some
 * ticks, and a pointer handler, and the dependency would cost more bundle than
 * the whole console spends on charts today.
 *
 * The palette is not chosen here by eye. Series colours come from the caller as
 * CSS custom properties so the light and dark steps swap with the theme rather
 * than one being a filter of the other, and both sets were validated for
 * colour-vision separation against this console's own surfaces before they were
 * written down — see `SERIES` in `serving-charts.tsx`.
 */
export interface ChartSeries {
  key: string
  label: string
  /** A CSS colour — a `var(--…)` the caller defined, so the theme swap is one place. */
  color: string
  points: MetricSeriesPoint[]
}

const PADDING = { top: 10, right: 12, bottom: 20, left: 52 }

/**
 * A tick label, compacted so it fits the gutter.
 *
 * `formatMetric` spells a rate in full — `1500.0/s` — which is right in a tile
 * where there is one number and room for it, and wrong on an axis, where eight
 * characters overflow the gutter and get cut off by the viewport. A clipped tick
 * is worse than a coarse one: `1500.0/s` cropped to `500.0/s` is not an ugly
 * label, it is a wrong number.
 */
function tickLabel(value: number, unit: MetricUnit): string {
  if (Math.abs(value) >= 1000) {
    const thousands = value / 1000
    const digits = Math.abs(thousands) >= 10 ? 0 : 1
    return `${thousands.toFixed(digits)}K${unit === "per_second" ? "/s" : ""}`
  }
  return formatMetric(value, unit)
}

/**
 * Y ticks on 1, 2 or 5 × a power of ten, so the axis reads in round numbers.
 *
 * The top tick is rounded *up* past the peak rather than stopping at the last
 * round number below it. Stopping below crops the tallest part of every curve —
 * the part somebody opened the chart to look at — and the crop is silent: the
 * line simply stops, which reads as missing data rather than as a clipped axis.
 */
function niceTicks(max: number, count = 4): number[] {
  if (!(max > 0)) return [0, 1]
  const raw = max / count
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const step =
    [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ??
    magnitude * 10
  const top = Math.ceil(max / step) * step
  const ticks: number[] = []
  for (let value = 0; value <= top + step * 0.001; value += step)
    ticks.push(value)
  return ticks
}

/**
 * The axis end labels.
 *
 * A span that crosses midnight carries the day, because two bare clock times a
 * day apart read as running backwards — `04:24 PM` on the left and `04:09 PM` on
 * the right looks like a mistake until you work out that they are 24 hours
 * apart, and nobody should have to.
 */
function clockLabel(at: number, span: number): string {
  const date = new Date(at * 1000)
  if (span > 36 * 3600) {
    return date.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    })
  }
  const time = date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  })
  if (span <= 6 * 3600) return time
  const day = date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  })
  return `${day} ${time}`
}

export function TimeChart({
  series,
  unit,
  height = 160,
  className,
}: {
  series: ChartSeries[]
  unit: MetricUnit
  height?: number
  className?: string
}) {
  const clipId = useId()
  const frame = useRef<SVGSVGElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<number | null>(null)
  const [width, setWidth] = useState(560)
  /**
   * The chart is drawn at the container's real pixel width, not scaled into it.
   *
   * A fixed viewBox stretched to fit is the obvious way and it is wrong here:
   * scaling multiplies everything, so the same chart at half the width has 5px
   * axis text and 1px strokes, and at double the width has fat lines and
   * oversized labels. Only the plot area should change with the container —
   * every mark and every label is a constant.
   */
  useLayoutEffect(() => {
    const element = box.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.round(entry.contentRect.width)
      if (next > 0) setWidth(next)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  const plot = {
    width: Math.max(width - PADDING.left - PADDING.right, 10),
    height: height - PADDING.top - PADDING.bottom,
  }

  const model = useMemo(() => {
    const drawn = series.filter((s) => s.points.length > 1)
    const all = drawn.flatMap((s) => s.points)
    if (all.length === 0) return null

    const times = all.map((p) => p.at)
    const minAt = Math.min(...times)
    const maxAt = Math.max(...times)
    const span = maxAt - minAt || 1
    const peak = Math.max(...all.map((p) => p.value), 0)
    const ticks = niceTicks(peak)
    const top = ticks[ticks.length - 1] || 1

    const x = (at: number) => ((at - minAt) / span) * plot.width
    const y = (value: number) => plot.height - (value / top) * plot.height

    return { drawn, minAt, span, ticks, x, y }
  }, [series, plot.width, plot.height])

  if (model === null) {
    return (
      <div
        ref={box}
        className={cn(
          "flex w-full items-center justify-center rounded-md border border-dashed text-2xs text-muted-foreground/60",
          className
        )}
        style={{ height }}
      >
        Not measured over this window
      </div>
    )
  }

  const { drawn, minAt, span, ticks, x, y } = model

  /**
   * The x the crosshair snapped to, and every series' value there.
   *
   * One readout listing every series, so the pointer never has to land on a line
   * to get a number — it only has to be near the right moment.
   */
  const readout =
    hover === null
      ? null
      : drawn.map((s) => {
          const nearest = s.points.reduce((best, point) =>
            Math.abs(point.at - hover) < Math.abs(best.at - hover)
              ? point
              : best
          )
          return { series: s, point: nearest }
        })

  const snapped = readout?.[0]?.point.at ?? null

  const onMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const box = frame.current?.getBoundingClientRect()
    if (!box) return
    const ratio = (event.clientX - box.left) / box.width
    const inPlot = ratio * width - PADDING.left
    setHover(minAt + (inPlot / plot.width) * span)
  }

  return (
    <div ref={box} className={cn("relative w-full", className)}>
      <svg
        ref={frame}
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        className="touch-none"
        role="img"
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        <defs>
          <clipPath id={clipId}>
            <rect x={0} y={-4} width={plot.width} height={plot.height + 4} />
          </clipPath>
        </defs>

        <g transform={`translate(${PADDING.left},${PADDING.top})`}>
          {ticks.map((tick) => (
            <g key={tick}>
              <line
                x1={0}
                x2={plot.width}
                y1={y(tick)}
                y2={y(tick)}
                className="stroke-border"
                strokeWidth={1}
              />
              <text
                x={-8}
                y={y(tick)}
                dominantBaseline="middle"
                textAnchor="end"
                className="fill-muted-foreground/70 font-mono text-[9px] tabular-nums"
              >
                {tickLabel(tick, unit)}
              </text>
            </g>
          ))}

          <g clipPath={`url(#${clipId})`}>
            {drawn.map((s) => (
              <path
                key={s.key}
                d={s.points
                  .map(
                    (p, i) =>
                      `${i === 0 ? "M" : "L"}${x(p.at).toFixed(1)},${y(p.value).toFixed(1)}`
                  )
                  .join(" ")}
                fill="none"
                stroke={s.color}
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ))}
          </g>

          {snapped !== null ? (
            <line
              x1={x(snapped)}
              x2={x(snapped)}
              y1={0}
              y2={plot.height}
              className="stroke-muted-foreground/40"
              strokeWidth={1}
            />
          ) : null}

          {readout?.map(({ series: s, point }) => (
            <circle
              key={s.key}
              cx={x(point.at)}
              cy={y(point.value)}
              r={4}
              fill={s.color}
              className="stroke-card"
              strokeWidth={2}
            />
          ))}

          <text
            x={0}
            y={plot.height + 14}
            className="fill-muted-foreground/70 font-mono text-[9px]"
          >
            {clockLabel(minAt, span)}
          </text>
          <text
            x={plot.width}
            y={plot.height + 14}
            textAnchor="end"
            className="fill-muted-foreground/70 font-mono text-[9px]"
          >
            {clockLabel(minAt + span, span)}
          </text>
        </g>
      </svg>

      {readout && snapped !== null ? (
        <div
          className="pointer-events-none absolute top-1 rounded-md border bg-popover px-2 py-1.5 shadow-sm"
          style={{
            left: `${((PADDING.left + x(snapped)) / width) * 100}%`,
            transform:
              x(snapped) > plot.width / 2
                ? "translateX(-105%)"
                : "translateX(5%)",
          }}
        >
          <p className="font-mono text-[9px] text-muted-foreground">
            {new Date(snapped * 1000).toLocaleString(undefined, {
              month: "short",
              day: "numeric",
              hour: "2-digit",
              minute: "2-digit",
            })}
          </p>
          {readout.map(({ series: s, point }) => (
            <p
              key={s.key}
              className="flex items-center gap-1.5 whitespace-nowrap"
            >
              <span
                aria-hidden
                className="inline-block h-0.5 w-2.5 rounded-full"
                style={{ background: s.color }}
              />
              <span className="font-mono text-2xs tabular-nums">
                {formatMetric(point.value, unit)}
              </span>
              <span className="text-2xs text-muted-foreground">{s.label}</span>
            </p>
          ))}
        </div>
      ) : null}
    </div>
  )
}

/** Identity for two or more series, mirroring the line mark rather than a filled box. */
export function ChartLegend({ series }: { series: ChartSeries[] }) {
  if (series.length < 2) return null
  return (
    <div className="flex flex-wrap items-center gap-3">
      {series.map((s) => (
        <span key={s.key} className="flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-0.5 w-3 rounded-full"
            style={{ background: s.color }}
          />
          <span className="text-2xs text-muted-foreground">{s.label}</span>
        </span>
      ))}
    </div>
  )
}
