import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  formatMetric,
  sampleOf,
  temperatureTone,
  valueOf,
} from "@/modules/metrics/format"
import { useMetrics } from "@/modules/metrics/queries"
import type { MetricScope, Metrics } from "@/modules/metrics/types"
import { IconChartLine } from "@tabler/icons-react"

import { MetricTile } from "./metric-tile"

const GPU_TILES = [
  "gpu.utilization",
  "gpu.memory_used",
  "gpu.temperature",
  "gpu.power",
  "gpu.sm_clock",
  "gpu.memory_utilization",
]

const INFERENCE_TILES = [
  "inference.ttft",
  "inference.tpot",
  "inference.output_tokens_per_second",
  "inference.input_tokens_per_second",
  "inference.requests_running",
  "inference.requests_waiting",
  "inference.kv_cache_utilization",
  "inference.latency",
]

function Empty({ reason }: { reason: string }) {
  /**
   * Unmeasured is a state, not a failure, and it is one the operator can fix —
   * so it says which knob, rather than showing an empty chart that reads as an
   * idle fleet.
   */
  return (
    <Card size="sm" className="items-start gap-1 px-3 py-3">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <IconChartLine className="size-4 shrink-0 opacity-60" />
        {reason}
      </div>
    </Card>
  )
}

function Breakdown({ data }: { data: Metrics }) {
  if (data.breakdown.length < 2) return null
  return (
    <Card size="sm" className="gap-0 overflow-hidden px-0">
      <div className="px-3 pb-2 font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        Per{" "}
        {data.scope === "cluster" || data.scope === "pool" ? "node" : "device"}
      </div>
      <div className="divide-y divide-border/60">
        {data.breakdown.map((member) => {
          const util = valueOf(member.samples, "gpu.utilization")
          const temp = valueOf(member.samples, "gpu.temperature")
          const tone = temperatureTone(temp)
          return (
            <div
              key={member.key}
              className="flex items-center gap-3 px-3 py-1.5 text-xs"
            >
              <span className="w-40 shrink-0 truncate font-mono text-2xs text-muted-foreground">
                {member.label}
              </span>
              <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-primary/70"
                  style={{ width: `${Math.min(100, Math.max(0, util ?? 0))}%` }}
                />
              </div>
              <span className="w-12 shrink-0 text-right font-mono tabular-nums">
                {formatMetric(util, "percent")}
              </span>
              <span
                className={cn(
                  "w-14 shrink-0 text-right font-mono tabular-nums",
                  tone === "danger"
                    ? "text-destructive"
                    : tone === "warn"
                      ? "text-amber-600 dark:text-amber-400"
                      : "text-muted-foreground"
                )}
              >
                {formatMetric(temp, "celsius")}
              </span>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

/**
 * Measurement for one object, kept separate from the object itself.
 *
 * It has its own query, its own refresh and its own failure: a Prometheus that
 * is unreachable must not make a node page look broken, because everything else
 * on that page is still true.
 */
export function MetricsPanel({
  scope,
  refId,
  family = "gpu",
  series = true,
  lead,
}: {
  scope: MetricScope
  refId: string | undefined
  family?: "gpu" | "inference"
  series?: boolean
  /**
   * Metric names to promote to full-size charts above the rest. A page that is
   * mostly about how a thing is performing names the two or three numbers that
   * mean that, and they stop competing with everything else for the same tile.
   */
  lead?: string[]
}) {
  const { data, isLoading, error } = useMetrics(scope, refId, { series })

  if (isLoading) {
    return (
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>
    )
  }
  if (error) return <Empty reason={formatError(error, "metrics unavailable")} />
  if (!data) return null
  if (data.unavailable) return <Empty reason={data.unavailable} />

  const wanted = family === "gpu" ? GPU_TILES : INFERENCE_TILES
  const tiles = wanted
    .map((name) => sampleOf(data.samples, name))
    .filter((s): s is NonNullable<typeof s> => Boolean(s))

  if (!tiles.length) {
    return (
      <Empty reason="the metrics store returned no series for this object" />
    )
  }

  const promoted = lead ?? []
  const led = promoted
    .map((name) => tiles.find((t) => t.metric === name))
    .filter((s): s is NonNullable<typeof s> => Boolean(s))
  const rest = tiles.filter((t) => !led.includes(t))

  const tile = (sample: (typeof tiles)[number], size: "sm" | "lg") => (
    <MetricTile
      key={sample.metric}
      sample={sample}
      size={size}
      tone={
        sample.metric === "gpu.temperature"
          ? temperatureTone(sample.value)
          : "ok"
      }
      points={data.series.find((s) => s.metric === sample.metric)?.points}
    />
  )

  return (
    <div className="flex flex-col gap-2">
      {led.length > 0 && (
        <div
          className={cn(
            "grid gap-2",
            led.length === 1
              ? "grid-cols-1"
              : led.length === 2
                ? "sm:grid-cols-2"
                : "sm:grid-cols-2 lg:grid-cols-3"
          )}
        >
          {led.map((sample) => tile(sample, "lg"))}
        </div>
      )}
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {rest.map((sample) => tile(sample, "sm"))}
      </div>
      <Breakdown data={data} />
      {data.source ? (
        <div className="font-mono text-2xs text-muted-foreground/50">
          {data.window} window · {data.source.kind} · {data.source.url}
        </div>
      ) : null}
    </div>
  )
}
