import { useState } from "react"
import { Panel } from "@/components/layout/panel"
import { Sparkline } from "@/components/metrics/sparkline"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { formatMetric } from "@/modules/metrics/format"
import { useMetrics } from "@/modules/metrics/queries"
import { sumCurvePoints } from "@/modules/metrics/rollup"
import { EmptyNote } from "./Common"

/**
 * The Overview traffic chart: how much work is actually arriving.
 *
 * `inference.requests_finished` is a rate the engines count and it sums across
 * clusters, so the curve is every cluster's share added at each step. It counts
 * no request that never reached a cluster, which is why this is a traffic chart
 * and not an availability one: the number that would answer the second lives
 * on the gateway's own series and this console does not read it yet.
 *
 * The range buttons are three different questions, not three shapes of one
 * answer: each re-asks the plane with its own span and step, because an hour
 * read at hourly resolution is one point.
 */
export function TrafficPanel({
  workloadId,
  placed,
}: {
  workloadId: string
  placed: boolean
}) {
  const [range, setRange] = useState<TrafficRange>("24h")
  const { span, step } = TRAFFIC_RANGES[range]
  const { data, isLoading, error } = useMetrics(
    "workload",
    placed ? workloadId : undefined,
    { series: true, span, step }
  )
  const points = sumCurvePoints(
    data?.series ?? [],
    "inference.requests_finished"
  )

  return (
    <Panel
      title="Traffic"
      description="Requests the engines completed per second, across every cluster"
      action={<RangeTabs value={range} onChange={setRange} />}
    >
      {!placed ? (
        <EmptyNote>
          this workload runs on no cluster, so no engine is counting requests
          for it
        </EmptyNote>
      ) : isLoading ? (
        <Skeleton className="h-24" />
      ) : error ? (
        <EmptyNote>{formatError(error, "traffic unavailable")}</EmptyNote>
      ) : points.length < 2 ? (
        <EmptyNote>
          {data?.unavailable ??
            `no request rate reported over the last ${range}`}
        </EmptyNote>
      ) : (
        <>
          <MiniChartLegend
            items={[
              {
                tone: "primary",
                label: "Completed",
                value: formatMetric(
                  points[points.length - 1].value,
                  "per_second"
                ),
              },
            ]}
          />
          <Sparkline points={points} size="lg" tone="primary" />
          <ChartAxis labels={trafficAxis(points)} />
        </>
      )}
    </Panel>
  )
}

/** Each range is its own query: an hour at hourly resolution is one point. */
export const TRAFFIC_RANGES = {
  "1h": { span: 3600, step: "1m" },
  "24h": { span: 24 * 3600, step: "5m" },
  "7d": { span: 7 * 24 * 3600, step: "1h" },
} as const

export type TrafficRange = keyof typeof TRAFFIC_RANGES

/** First, middle and last stamp of what actually came back, in local time. */
export function trafficAxis(points: { at: number }[]): string[] {
  return [0, Math.floor(points.length / 2), points.length - 1].map((i) =>
    new Date(points[i].at * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    })
  )
}

export function RangeTabs({
  value,
  onChange,
}: {
  value: TrafficRange
  onChange: (r: TrafficRange) => void
}) {
  return (
    <div className="flex gap-0.5 rounded-md bg-muted p-0.5">
      {(Object.keys(TRAFFIC_RANGES) as TrafficRange[]).map((r) => (
        <button
          key={r}
          type="button"
          onClick={() => onChange(r)}
          className={cn(
            "rounded px-2 py-0.5 font-mono text-2xs",
            value === r
              ? "bg-card text-foreground"
              : "text-muted-foreground hover:text-foreground"
          )}
        >
          {r}
        </button>
      ))}
    </div>
  )
}

export const STROKE = {
  primary: "stroke-primary",
  warn: "stroke-amber-500",
  danger: "stroke-destructive",
} as const

export const DOT = {
  primary: "bg-primary",
  warn: "bg-amber-500",
  danger: "bg-destructive",
} as const

/**
 * A small grid-backed line chart taking up to two series on a shared scale, so
 * that two measurements read against one axis. Not the
 * `Sparkline`, which auto-scales each series on its own and would misalign a
 * pair. Pure SVG, no charting dependency.
 */
export function MiniChart({
  lines,
}: {
  lines: { points: number[]; tone: keyof typeof STROKE }[]
}) {
  const all = lines.flatMap((l) => l.points)
  const max = Math.max(...all, 1)
  const min = Math.min(...all, 0)
  const span = max - min || 1
  const w = 560
  const h = 130
  const toPoints = (pts: number[]) =>
    pts
      .map(
        (v, i) =>
          `${((i / Math.max(pts.length - 1, 1)) * w).toFixed(1)},${(
            h -
            ((v - min) / span) * (h - 16) -
            8
          ).toFixed(1)}`
      )
      .join(" ")
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      className="h-32 w-full"
      aria-hidden
    >
      {[10, 45, 80, 115].map((y) => (
        <line
          key={y}
          x1={0}
          y1={y}
          x2={w}
          y2={y}
          className="stroke-border"
          strokeWidth={1}
        />
      ))}
      {lines.map((line, i) => (
        <polyline
          key={i}
          points={toPoints(line.points)}
          fill="none"
          strokeWidth={1.75}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
          className={STROKE[line.tone]}
        />
      ))}
    </svg>
  )
}

export function MiniChartLegend({
  items,
}: {
  items: { tone: keyof typeof DOT; label: string; value: string }[]
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-3.5">
      {items.map((item) => (
        <span
          key={item.label}
          className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground"
        >
          <span className={cn("size-2 rounded-full", DOT[item.tone])} />
          {item.label}{" "}
          <span className="font-mono text-foreground">{item.value}</span>
        </span>
      ))}
    </div>
  )
}

export function ChartAxis({ labels }: { labels: string[] }) {
  return (
    <div className="mt-1 flex justify-between font-mono text-2xs text-muted-foreground">
      {labels.map((label, i) => (
        <span key={i}>{label}</span>
      ))}
    </div>
  )
}
