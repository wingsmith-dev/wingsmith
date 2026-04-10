import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { formatMetric } from "@/modules/metrics/format"
import type { MetricSample, MetricSeriesPoint } from "@/modules/metrics/types"

import { Sparkline } from "./sparkline"

const TONE = {
  ok: "text-foreground",
  warn: "text-amber-600 dark:text-amber-400",
  danger: "text-destructive",
} as const

export type MetricTone = keyof typeof TONE

export function MetricTile({
  sample,
  tone = "ok",
  points,
  footnote,
  size = "sm",
}: {
  sample: MetricSample | undefined
  tone?: MetricTone
  points?: MetricSeriesPoint[]
  footnote?: string
  /** `lg` is for the handful of metrics a page is actually about. */
  size?: "sm" | "lg"
}) {
  if (!sample) return null
  const absent = sample.value === null
  const large = size === "lg"
  return (
    <Card size="sm" className={cn("gap-1.5 px-3", large && "gap-2 py-4")}>
      <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {sample.title}
      </div>
      <div
        className={cn(
          "font-mono leading-none tabular-nums",
          large ? "text-3xl" : "text-lg",
          absent ? "text-muted-foreground/40" : TONE[tone]
        )}
      >
        {formatMetric(sample.value, sample.unit)}
      </div>
      {points && points.length > 1 ? (
        <Sparkline
          points={points}
          size={size}
          tone={tone === "ok" ? "primary" : tone}
        />
      ) : null}
      {footnote ? (
        <div className="text-2xs text-muted-foreground/60">{footnote}</div>
      ) : null}
    </Card>
  )
}
