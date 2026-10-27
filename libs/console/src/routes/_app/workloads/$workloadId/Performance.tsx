import { DataTable } from "@/components/layout/data-table"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { Panel } from "@/components/layout/panel"
import { StatRow, StatTile } from "@/components/layout/stat-tile"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { WorkloadPlacementNode } from "@/modules/demand/types"
import { formatMetric, sampleOf, valueOf } from "@/modules/metrics/format"
import { useMetrics } from "@/modules/metrics/queries"
import {
  idleGpuHours,
  latencyStages,
  meanUtilization,
  sumCurves,
  utilizationWeek,
  type LatencyStage,
  type UtilizationWeek,
} from "@/modules/metrics/rollup"
import type { Metrics } from "@/modules/metrics/types"
import { EmptyNote, MetricsNotice, staticList, mono } from "./Common"
import { MiniChart, MiniChartLegend } from "./Traffic"

/* ---- performance ------------------------------------------------------ */

/** A week of hourly buckets, which is what the heatmap draws. */
export const HEATMAP_SPAN = 7 * 24 * 3600

export const HEATMAP_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

/**
 * How the workload is performing, everywhere placement put it.
 *
 * A workload is the one object that spans clusters, and the plane answers it as
 * its own scope, not as an average this console computes: throughputs
 * and counts add, the hottest card wins, and a quantile or a mean that cannot
 * cross clusters accurately comes back unmeasured with every cluster's own figure
 * in the breakdown. So nothing here re-derives a merged number: a tile that
 * reads "—" is the plane refusing, and that refusal is information.
 *
 * Two requests, not six panels' worth: one for the live window every tile and
 * the two charts read, and one long, coarse one for the week the heatmap draws.
 */
export function PerformanceView({
  workloadId,
  placed,
  gpuCount,
}: {
  workloadId: string
  placed: boolean
  gpuCount: number
}) {
  const live = useMetrics("workload", placed ? workloadId : undefined, {
    series: true,
  })
  const week = useMetrics("workload", placed ? workloadId : undefined, {
    series: true,
    span: HEATMAP_SPAN,
    step: "1h",
  })

  if (!placed) {
    return (
      <MetricsNotice>
        This workload runs on no cluster, so nothing is measuring it.
        Performance appears once placement admits it somewhere.
      </MetricsNotice>
    )
  }
  if (live.isLoading) {
    return (
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {[0, 1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-16" />
        ))}
      </div>
    )
  }
  if (live.error) {
    return (
      <MetricsNotice>
        {formatError(live.error, "metrics unavailable")}
      </MetricsNotice>
    )
  }
  if (!live.data) return null
  /**
   * A workload spans clusters, so `unavailable` and real numbers arrive
   * together: one cluster's store being down is a reason, not an empty page,
   * and the clusters that did answer are still measured. Only an answer with
   * nothing in it at all becomes the notice.
   */
  if (live.data.unavailable && live.data.samples.length === 0) {
    return <MetricsNotice>{live.data.unavailable}</MetricsNotice>
  }

  const data = live.data
  const out = sumCurves(data.series, "inference.output_tokens_per_second")
  const input = sumCurves(data.series, "inference.input_tokens_per_second")
  const week7 = utilizationWeek(week.data)
  const idleHours = idleGpuHours(week.data, gpuCount, HEATMAP_SPAN)

  return (
    <>
      <StatRow>
        <StatTile
          label="GPU utilization"
          value={reading(data, "gpu.utilization")}
        />
        <StatTile
          label="KV cache utilization"
          value={reading(data, "inference.kv_cache_utilization")}
        />
        <StatTile
          label="Requests running"
          value={reading(data, "inference.requests_running")}
        />
        <StatTile
          label="Tokens / sec"
          value={reading(data, "inference.output_tokens_per_second")}
        />
        <StatTile
          label="Requests queued"
          value={reading(data, "inference.requests_waiting")}
        />
      </StatRow>

      <div className="grid gap-4 md:grid-cols-[2fr_1fr]">
        <div className="flex min-w-0 flex-col gap-4">
          <Panel
            title="Throughput"
            description="Input and output tokens per second, summed over every replica"
          >
            {out.length > 1 || input.length > 1 ? (
              <>
                <MiniChartLegend
                  items={[
                    {
                      tone: "primary",
                      label: "Output",
                      value: reading(
                        data,
                        "inference.output_tokens_per_second"
                      ),
                    },
                    {
                      tone: "warn",
                      label: "Input",
                      value: reading(data, "inference.input_tokens_per_second"),
                    },
                  ]}
                />
                <MiniChart
                  lines={[
                    { points: out, tone: "primary" },
                    { points: input, tone: "warn" },
                  ]}
                />
              </>
            ) : (
              <EmptyNote>
                the metrics store returned no token throughput series for this
                window
              </EmptyNote>
            )}
          </Panel>

          <Panel
            title="Latency breakdown"
            description="Where a request spends its time, from the engines serving it"
          >
            <DataTable
              columns={latencyColumns}
              list={staticList(
                latencyStages(data.samples).map((stage) => ({
                  ...stage,
                  value: reading(data, stage.metric),
                }))
              )}
              rowKey={(r) => r.metric}
              empty="No latency series in this window."
            />
          </Panel>

          <Panel
            title="GPU utilization"
            description="Hourly average across the cards this workload holds, last 7 days"
            action={<HeatmapLegend />}
          >
            {week.isLoading ? (
              <Skeleton className="h-40" />
            ) : week.error ? (
              <EmptyNote>
                {formatError(week.error, "the week could not be read")}
              </EmptyNote>
            ) : week7.length === 0 ? (
              <EmptyNote>
                {week.data?.unavailable ??
                  "no hourly utilization series for the last 7 days"}
              </EmptyNote>
            ) : (
              week7.map((curve) => (
                <GpuHeatmap key={curve.label} curve={curve} />
              ))
            )}
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Panel title="Resource utilization">
            <FieldGrid>
              <Field label="Framebuffer used">
                {reading(data, "gpu.memory_used")}
              </Field>
              <Field label="Framebuffer free">
                {reading(data, "gpu.memory_free")}
              </Field>
              <Field label="Memory bandwidth">
                {reading(data, "gpu.memory_utilization")}
              </Field>
              <Field label="SM clock">{reading(data, "gpu.sm_clock")}</Field>
              <Field label="Power draw">{reading(data, "gpu.power")}</Field>
              <Field label="GPU temperature">
                {reading(data, "gpu.temperature")}
              </Field>
            </FieldGrid>
          </Panel>

          <Panel
            title="Efficiency"
            description="Derived from the utilization series above and the cards placement holds"
          >
            <FieldGrid>
              <Field label="Mean utilization (7d)">
                {formatMetric(meanUtilization(week.data), "percent")}
              </Field>
              <Field label="Idle GPU-hours (7d)">
                {idleHours === null ? (
                  <Dash>{null}</Dash>
                ) : (
                  idleHours.toFixed(1)
                )}
              </Field>
              <Field label="Cards held">
                {gpuCount ? String(gpuCount) : <Dash>{null}</Dash>}
              </Field>
              <Field label="Window">{data.window}</Field>
            </FieldGrid>
          </Panel>

          {data.breakdown.length > 1 && (
            <Panel
              title="Per cluster"
              description="Each cluster's own figures, which is where a number that cannot cross clusters survives"
            >
              <div className="flex flex-col gap-3">
                {data.breakdown.map((member) => (
                  <div key={member.key} className="flex flex-col gap-1">
                    <div className="font-mono text-2xs text-muted-foreground">
                      {member.label}
                    </div>
                    <FieldGrid>
                      <Field label="GPU utilization">
                        {formatMetric(
                          valueOf(member.samples, "gpu.utilization"),
                          "percent"
                        )}
                      </Field>
                      <Field label="Tokens / sec">
                        {formatMetric(
                          valueOf(
                            member.samples,
                            "inference.output_tokens_per_second"
                          ),
                          "per_second"
                        )}
                      </Field>
                    </FieldGrid>
                  </div>
                ))}
              </div>
            </Panel>
          )}
        </div>
      </div>

      {data.unavailable && (
        <div className="text-2xs text-muted-foreground">
          Partly measured: {data.unavailable}
        </div>
      )}

      {data.source && (
        <div className="font-mono text-2xs text-muted-foreground">
          {data.window} window · {data.source.kind} · {data.source.url}
        </div>
      )}
    </>
  )
}

/** One metric as the console spells it, or an em dash where nothing measured it. */
export function reading(data: Metrics, metric: string): string {
  const sample = sampleOf(data.samples, metric)
  return sample ? formatMetric(sample.value, sample.unit) : "—"
}

/** A stage the store answered, with its figure already rendered. */
export type LatencyRow = LatencyStage & { value: string }

export const latencyColumns = [
  {
    key: "stage",
    header: "Stage",
    cell: (r: LatencyStage) => <span className="text-xs">{r.stage}</span>,
  },
  {
    key: "metric",
    header: "Metric",
    secondary: true,
    cell: (r: LatencyStage) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {r.metric}
      </span>
    ),
  },
  {
    key: "value",
    header: "Value",
    align: "right" as const,
    cell: (r: LatencyRow) => mono(r.value),
  },
]

export function GpuHeatmap({ curve }: { curve: UtilizationWeek }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="font-mono text-2xs text-muted-foreground">
        {curve.label}
      </div>
      {HEATMAP_DAYS.map((label, day) => (
        <div key={label} className="flex items-center gap-2.5">
          <span className="w-8 shrink-0 font-mono text-2xs text-muted-foreground">
            {label}
          </span>
          <div className="grid flex-1 grid-cols-[repeat(24,minmax(0,1fr))] gap-0.5">
            {Array.from({ length: 24 }, (_, hour) => {
              const pct = curve.grid[day][hour]
              return (
                <span
                  key={hour}
                  title={
                    pct === null
                      ? `${label} ${String(hour).padStart(2, "0")}:00 · not reported`
                      : `${label} ${String(hour).padStart(2, "0")}:00 · ${Math.round(pct)}% GPU util`
                  }
                  className={cn(
                    "aspect-square rounded-[2px]",
                    pct === null && "border border-dashed border-border"
                  )}
                  style={
                    pct === null
                      ? undefined
                      : {
                          backgroundColor: `color-mix(in oklab, var(--primary) ${Math.round(
                            Math.min(100, Math.max(0, pct)) * 0.75
                          )}%, var(--secondary))`,
                        }
                  }
                />
              )
            })}
          </div>
        </div>
      ))}
      <div className="flex justify-between pl-[2.625rem] font-mono text-2xs text-muted-foreground">
        <span>00:00</span>
        <span>06:00</span>
        <span>12:00</span>
        <span>18:00</span>
        <span>23:00</span>
      </div>
    </div>
  )
}

export function HeatmapLegend() {
  return (
    <div className="flex items-center gap-1.5 text-2xs text-muted-foreground">
      Low
      <span className="flex gap-0.5">
        {[12, 32, 52, 72, 92].map((pct) => (
          <span
            key={pct}
            className="size-2.5 rounded-[2px]"
            style={{
              backgroundColor: `color-mix(in oklab, var(--primary) ${pct}%, var(--secondary))`,
            }}
          />
        ))}
      </span>
      High
    </div>
  )
}

/* ---- placement topology --------------------------------------------- */

/**
 * The workload's own occupancy of one node, from `/placement`. The plane
 * reports how many of the node's devices this workload holds; it does not
 * report the node's total capacity or what other workloads hold, so the card
 * draws the held devices and no idle or foreign slots.
 */
export function slotStyle(): React.CSSProperties {
  return {
    backgroundColor: "color-mix(in oklab, var(--primary) 55%, transparent)",
    borderColor: "var(--primary)",
  }
}

export function GpuNodeCard({ node }: { node: WorkloadPlacementNode }) {
  return (
    <div className="flex flex-col gap-2.5 rounded-md border bg-muted/40 px-3.5 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-xs font-semibold">{node.node}</span>
        <span className="font-mono text-2xs text-muted-foreground">
          {node.cluster} · {node.device_count} GPU
          {node.device_count === 1 ? "" : "s"} held
        </span>
      </div>
      <div className="grid grid-cols-8 gap-1">
        {Array.from({ length: node.device_count }, (_, i) => (
          <span
            key={i}
            className="aspect-square rounded border"
            style={slotStyle()}
          />
        ))}
      </div>
    </div>
  )
}

export function LegendSwatch({ children }: React.PropsWithChildren) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block size-2.5 rounded-[2px] border"
        style={slotStyle()}
      />
      {children}
    </span>
  )
}
