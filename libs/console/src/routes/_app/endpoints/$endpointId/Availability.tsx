import { STAT_TONE_CLASS, type StatTone } from "@/components/layout/stat-tone"
import { MetricChart, type CurveTone } from "@/components/metrics/sparkline"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { formatMetric, sampleOf } from "@/modules/metrics/format"
import { sumCurvePoints, type AvailabilityDay } from "@/modules/metrics/rollup"
import type { Metrics } from "@/modules/metrics/types"

/** The strip's width, in days. */
export const AVAILABILITY_DAYS = 14

export const AVAILABILITY_SPAN = AVAILABILITY_DAYS * 86400

/** One metric as the console spells it, or an em dash where nothing measured it. */
export function reading(data: Metrics | undefined, metric: string): string {
  if (!data) return "—"
  const sample = sampleOf(data.samples, metric)
  return sample ? formatMetric(sample.value, sample.unit) : "—"
}

/** Why a chart or a strip is empty, said where the chart would have been. */
export function EmptyNote({ children }: React.PropsWithChildren) {
  return (
    <p className="py-2 font-mono text-2xs text-muted-foreground">{children}</p>
  )
}

/**
 * A chart of one or more metrics, or a note saying the store held none.
 *
 * A chart draws nothing at all below two points, so without this a metric
 * nobody is collecting and a metric that is flat at zero look identical.
 *
 * The unit comes off the first metric's series, and every metric passed
 * together must share it — they are drawn against one axis, and a chart mixing
 * seconds with a rate would label that axis with a lie.
 */
export function Chart({
  data,
  curves,
  spanLabel,
  fill,
  label,
}: {
  data: Metrics
  curves: { metric: string; tone: CurveTone }[]
  spanLabel: string
  fill?: boolean
  label: string
}) {
  const drawn = curves.map((curve) => ({
    tone: curve.tone,
    points: sumCurvePoints(data.series, curve.metric),
  }))
  if (drawn.every((curve) => curve.points.length < 2)) {
    return <EmptyNote>no {label} series for this window</EmptyNote>
  }
  /**
   * The unit off the first metric the store actually answered, not off the
   * first one asked for: a p99 nobody collected would otherwise label the axis
   * of the p50 drawn beside it.
   */
  const named = new Set(curves.map((curve) => curve.metric))
  const unit =
    data.series.find(
      (series) => named.has(series.metric) && series.points.length > 0
    )?.unit ?? "count"
  return (
    <MetricChart curves={drawn} unit={unit} spanLabel={spanLabel} fill={fill} />
  )
}

/**
 * Where the console draws the line on an availability figure.
 *
 * The measurement carries no threshold of its own — whether 99.4% is an outage
 * is a claim somebody made, and the plane deliberately declines to make it. The
 * two numbers here are this console's reading colour and nothing else; they are
 * not an SLO and no acceptance term is checked against them.
 */
export function availabilityTone(percent: number | null): StatTone {
  if (percent === null) return "default"
  if (percent >= 99.9) return "ok"
  return "warn"
}

/**
 * The availability strip, one cell per day.
 *
 * A day the store answered nothing is drawn hollow rather than red. An endpoint
 * nobody called, and an endpoint the gateway's metrics port was never scraped
 * for, were not unavailable — and a strip that says they were is the one thing
 * this cell must never do.
 *
 * The cells divide the width they are given, because `availabilityDays` always
 * returns the full fortnight: the strip is the same shape whatever the store
 * answered, and only the colours change.
 */
export function AvailabilityBar({ days }: { days: AvailabilityDay[] }) {
  return (
    <div className="flex items-end gap-[3px]">
      {days.map((day) => (
        <span
          key={day.at}
          title={`${new Date(day.at * 1000).toLocaleDateString()} · ${formatMetric(day.value, "percent")}`}
          className={cn(
            "h-7 flex-1 rounded-[2px]",
            day.value === null
              ? "bg-muted-foreground/15"
              : day.value >= 99.9
                ? "bg-emerald-500"
                : day.value >= 99
                  ? "bg-amber-500"
                  : "bg-destructive"
          )}
        />
      ))}
    </div>
  )
}

/**
 * Availability twice: over the span the toggle names, and over the fortnight.
 *
 * The headline obeys the toggle, because a fortnight-wide figure is the wrong
 * answer to "how is this name doing" — a name that has been failing for an hour
 * still reads green across fourteen days, and that is precisely the moment
 * somebody is looking at this page.
 *
 * The strip below it stays fixed at fourteen days and says so. It is the other
 * question — not what this name is doing, but how it has behaved — which is why
 * it sits above the log rather than in the stat strip.
 *
 * Both are `100 - failure_ratio`, counted over requests and not over time: this
 * is the share of calls the front door answered, and an endpoint nobody called
 * has no figure at all rather than a perfect one. That is why an absent reading
 * is an em dash here and must never be drawn as 100%.
 */
export function AvailabilityCard({
  windowLabel,
  loading,
  percent,
  historyLoading,
  history,
  days,
}: {
  windowLabel: string
  loading: boolean
  percent: number | null
  historyLoading: boolean
  history: number | null
  days: AvailabilityDay[]
}) {
  return (
    <Card size="sm" className="gap-2.5 px-4 py-3.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          Availability
        </span>
        <span className="font-mono text-2xs text-muted-foreground">
          {windowLabel}
        </span>
      </div>
      <div
        className={cn(
          "font-mono text-xl leading-none tabular-nums",
          STAT_TONE_CLASS[availabilityTone(percent)]
        )}
      >
        {loading ? "…" : formatMetric(percent, "percent")}
      </div>
      <p className="font-mono text-2xs text-muted-foreground">
        the share of calls the front door answered
      </p>

      <div className="space-y-1.5 border-t pt-3">
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
            last {AVAILABILITY_DAYS} days
          </span>
          <span
            className={cn(
              "font-mono text-2xs tabular-nums",
              STAT_TONE_CLASS[availabilityTone(history)]
            )}
          >
            {historyLoading ? "…" : formatMetric(history, "percent")}
          </span>
        </div>
        <div className="w-full max-w-[420px] space-y-1.5">
          <AvailabilityBar days={days} />
          <div className="flex items-center justify-between font-mono text-2xs text-muted-foreground">
            <span>{AVAILABILITY_DAYS}d ago</span>
            <span>today</span>
          </div>
        </div>
      </div>
    </Card>
  )
}
