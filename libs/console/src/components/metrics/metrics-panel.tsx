import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  AVAILABILITY_SOURCE,
  DEFAULT_TILES,
  metricDefinition,
  metricGroups,
  type MetricFamily,
} from "@/modules/metrics/catalog"
import {
  formatMetric,
  sampleOf,
  temperatureTone,
  valueOf,
} from "@/modules/metrics/format"
import { useMetrics } from "@/modules/metrics/queries"
import type { MetricScope, Metrics } from "@/modules/metrics/types"
import { IconChartLine } from "@tabler/icons-react"
import { useState } from "react"

import { MetricPicker } from "./metric-picker"
import { MetricTile } from "./metric-tile"

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

/** What one member of a breakdown is, said in the word the reader uses. */
const MEMBER: Record<MetricScope, string> = {
  cluster: "node",
  pool: "node",
  node: "device",
  device: "device",
  deployment: "device",
  workload: "cluster",
  endpoint: "cluster",
  tenant: "endpoint",
}

/**
 * The two numbers a breakdown row carries, per family.
 *
 * The bar is always a percent, because a bar drawn from anything else compares
 * members on a scale that does not exist. The figure beside it is the one thing
 * that says "go look at this member" — heat for cards, the tail for serving.
 */
const ROW: Record<MetricFamily, { bar: string; side: string }> = {
  gpu: { bar: "gpu.utilization", side: "gpu.temperature" },
  inference: {
    bar: "inference.kv_cache_utilization",
    side: "inference.ttft",
  },
  serving: { bar: "serving.availability", side: "serving.latency_p99" },
}

function Breakdown({ data, family }: { data: Metrics; family: MetricFamily }) {
  if (data.breakdown.length < 2) return null
  const row = ROW[family]
  const side = metricDefinition(row.side)
  return (
    <Card size="sm" className="gap-0 overflow-hidden px-0">
      <div className="px-3 pb-2 font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        Per {MEMBER[data.scope]}
      </div>
      <div className="divide-y divide-border/60">
        {data.breakdown.map((member) => {
          const bar = valueOf(member.samples, row.bar)
          const value = valueOf(member.samples, row.side)
          const tone =
            row.side === "gpu.temperature" ? temperatureTone(value) : "ok"
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
                  style={{ width: `${Math.min(100, Math.max(0, bar ?? 0))}%` }}
                />
              </div>
              <span className="w-12 shrink-0 text-right font-mono tabular-nums">
                {formatMetric(bar, "percent")}
              </span>
              <span
                className={cn(
                  "w-14 shrink-0 text-right font-mono tabular-nums",
                  tone === "danger"
                    ? "text-destructive"
                    : tone === "warn"
                      ? "text-warning"
                      : "text-muted-foreground"
                )}
              >
                {formatMetric(value, side?.unit ?? "count")}
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
 *
 * Everything it draws is observed, so nothing here edits anything. The picker
 * changes what this reader looks at, which is not a change to the fleet.
 */
export function MetricsPanel({
  scope,
  refId,
  family = "gpu",
  series = true,
  lead,
  leadOnly = false,
}: {
  scope: MetricScope
  refId: string | undefined
  family?: MetricFamily
  series?: boolean
  /**
   * Metric names to promote to full-size charts above the rest. A page that is
   * mostly about how a thing is performing names the two or three numbers that
   * mean that, and they stop competing with everything else for the same tile.
   *
   * Three is the number that fills the row. One is given the whole width, which
   * draws a sparkline as a stripe across an empty card.
   */
  lead?: string[]
  /**
   * Draw the lead row and nothing else — no tiles, no breakdown, no picker.
   *
   * For a summary that sits above other content and answers whether to go and
   * look properly. The full panel belongs on the page that is about measurement;
   * an overview carrying all of it is the reason a page needs tabs.
   */
  leadOnly?: boolean
}) {
  const { data, isLoading, error } = useMetrics(scope, refId, { series })
  const [selected, setSelected] = useState<string[]>(DEFAULT_TILES[family])

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

  const promoted = new Set(lead ?? [])
  const led = (lead ?? [])
    .map((name) => sampleOf(data.samples, name))
    .filter((s): s is NonNullable<typeof s> => Boolean(s))

  const groups = metricGroups(family, selected)
    .map((group) => ({
      ...group,
      samples: group.metrics
        .filter((metric) => !promoted.has(metric.name))
        .map((metric) => sampleOf(data.samples, metric.name))
        .filter((s): s is NonNullable<typeof s> => Boolean(s)),
    }))
    .filter((group) => group.samples.length > 0)

  if (led.length === 0 && groups.length === 0) {
    return (
      <Empty reason="the metrics store returned no series for this object" />
    )
  }

  const tile = (sample: (typeof data.samples)[number], size: "sm" | "lg") => (
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
      {!leadOnly && (
        <div className="flex justify-end">
          <MetricPicker
            family={family}
            selected={selected}
            available={data.samples.map((sample) => sample.metric)}
            onChange={setSelected}
          />
        </div>
      )}

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

      {!leadOnly &&
        groups.map((group) => (
          <div key={group.group} className="flex flex-col gap-1.5">
            {groups.length > 1 && (
              <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
                {group.label}
              </div>
            )}
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {group.samples.map((sample) => tile(sample, "sm"))}
            </div>
          </div>
        ))}

      {!leadOnly && <Breakdown data={data} family={family} />}
      {!leadOnly && data.source ? (
        <div className="font-mono text-2xs text-muted-foreground">
          {data.window} window · {data.source.kind} · {data.source.url}
        </div>
      ) : null}
      {!leadOnly && family === "inference" ? (
        <p className="max-w-3xl text-2xs text-muted-foreground">
          {AVAILABILITY_SOURCE}
        </p>
      ) : null}
    </div>
  )
}
