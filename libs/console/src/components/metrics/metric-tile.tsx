import { Card } from "@/components/ui/card"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { metricDefinition } from "@/modules/metrics/catalogue"
import { formatMetric } from "@/modules/metrics/format"
import type { MetricSample, MetricSeriesPoint } from "@/modules/metrics/types"

import { Sparkline } from "./sparkline"

const TONE = {
  ok: "text-foreground",
  warn: "text-warning",
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
  const definition = metricDefinition(sample.metric)
  const title = definition?.title ?? sample.title
  const note = definition?.note

  return (
    <Card size="sm" className={cn("gap-1.5 px-3", large && "gap-2 py-4")}>
      {note ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <div
              title={note}
              className="w-fit cursor-help font-mono text-2xs tracking-wider text-muted-foreground uppercase underline decoration-dotted underline-offset-2"
            >
              {title}
            </div>
          </TooltipTrigger>
          <TooltipContent className="max-w-72">{note}</TooltipContent>
        </Tooltip>
      ) : (
        <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          {title}
        </div>
      )}
      <div
        className={cn(
          "font-mono leading-none tabular-nums",
          large ? "text-3xl" : "text-lg",
          absent ? "text-muted-foreground" : TONE[tone]
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
        <div className="text-2xs text-muted-foreground">{footnote}</div>
      ) : null}
    </Card>
  )
}
