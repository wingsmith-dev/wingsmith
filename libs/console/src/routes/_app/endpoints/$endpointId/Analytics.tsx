import { Field, FieldGrid } from "@/components/layout/field"
import { StatTile } from "@/components/layout/stat-tile"
import { WindowToggle } from "@/components/metrics/window-toggle"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { sampleOf } from "@/modules/metrics/format"
import { useMetrics } from "@/modules/metrics/queries"
import { availabilityDays } from "@/modules/metrics/rollup"
import type { TimeWindow } from "@/modules/metrics/windows"
import { reading, EmptyNote, Chart, AvailabilityCard } from "./Availability"
import { SlaCard } from "./SlaCard"

/** The analytics tab: what the gateway measured for this endpoint. */
export function AnalyticsView({
  endpointId,
  chartWindow,
  setChartWindow,
  live,
  fortnight,
  days,
  availableNow,
  availableFortnight,
  failure,
}: {
  endpointId: string
  chartWindow: TimeWindow
  setChartWindow: (window: TimeWindow) => void
  live: ReturnType<typeof useMetrics>
  fortnight: ReturnType<typeof useMetrics>
  days: ReturnType<typeof availabilityDays>
  availableNow: ReturnType<typeof sampleOf>
  availableFortnight: ReturnType<typeof sampleOf>
  failure: ReturnType<typeof sampleOf>
}) {
  const metrics = live.data
  /**
   * Why the measurement half of this page is empty, when it is.
   *
   * Three different absences, and they must not be spelled the same way. No
   * store configured, a store nobody pointed at the gateway, and a store that
   * answered with no series are told apart by the plane and reported verbatim;
   * an endpoint nobody has called is not 100% available and is not 0% either.
   */
  const unmeasured = live.error
    ? formatError(live.error, "metrics unavailable")
    : (metrics?.unavailable ?? null)
  return (
    <>
      {/* The gateway's own series, read at the front door. Absent
        measurements render as an em dash, never as a zero. The toggle
        governs the stat strip and both charts under it, because the
        tiles are the same query's instant values — a tile read over
        one lookback beside a curve drawn over another is two
        measurements wearing one heading. Availability is not in that
        strip and names its own span, for that same reason. */}
      <div className="flex items-center justify-end gap-2">
        <WindowToggle value={chartWindow} onChange={setChartWindow} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          {/* The strip carries only what no card below it repeats: a
            rate, the percentile people scan for, and the failure
            share. p50 and p95 are in the latency card's legend, and
            putting them here as well is the same number twice. */}
          {live.isLoading ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-[68px]" />
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <StatTile
                label="Requests / sec"
                value={reading(metrics, "serving.requests_per_second")}
              />
              <StatTile
                label="p99 latency"
                value={reading(metrics, "serving.latency_p99")}
              />
              <StatTile
                label="Failed requests"
                value={reading(metrics, "serving.failure_ratio")}
                tone={
                  failure?.value == null
                    ? "default"
                    : failure.value > 1
                      ? "danger"
                      : failure.value > 0
                        ? "warn"
                        : "ok"
                }
              />
            </div>
          )}

          {unmeasured && (
            <Card size="sm" className="px-3 py-2.5">
              <p className="font-mono text-2xs text-muted-foreground">
                {unmeasured}
              </p>
            </Card>
          )}

          {/* Traffic — the gateway's request rate for this name. */}
          <Card>
            <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
              <div>
                <CardTitle className="text-sm">Traffic</CardTitle>
                <p className="text-2xs text-muted-foreground">
                  Requests per second, counted at the front door
                </p>
              </div>
              <span className="font-mono text-2xs text-muted-foreground">
                {chartWindow.label}
              </span>
            </CardHeader>
            <CardContent>
              {live.isLoading ? (
                <Skeleton className="h-28" />
              ) : metrics ? (
                <Chart
                  data={metrics}
                  curves={[
                    {
                      metric: "serving.requests_per_second",
                      tone: "primary",
                    },
                  ]}
                  spanLabel={chartWindow.label}
                  fill
                  label="request rate"
                />
              ) : (
                <EmptyNote>nothing is measuring this endpoint</EmptyNote>
              )}
            </CardContent>
          </Card>

          {/* Latency — the gateway's percentiles, which include the hop
            and every request that failed before an engine saw it. Not
            the same measurement as a deployment's
            `inference.latency`, and the two disagreeing is not a
            fault. */}
          <Card>
            <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
              <div>
                <CardTitle className="text-sm">Latency</CardTitle>
                <p className="text-2xs text-muted-foreground">
                  End-to-end percentiles, hop and queue included
                </p>
              </div>
              <span className="font-mono text-2xs text-muted-foreground">
                {chartWindow.label}
              </span>
            </CardHeader>
            <CardContent className="space-y-3">
              {live.isLoading ? (
                <Skeleton className="h-24" />
              ) : metrics ? (
                <>
                  <Chart
                    data={metrics}
                    curves={[
                      { metric: "serving.latency_p99", tone: "danger" },
                      { metric: "serving.latency_p95", tone: "warn" },
                      {
                        metric: "serving.latency_p50",
                        tone: "primary",
                      },
                    ]}
                    spanLabel={chartWindow.label}
                    label="latency"
                  />
                  <div className="flex flex-wrap items-center gap-4 pl-[72px] text-2xs text-muted-foreground">
                    <span className="inline-flex items-center gap-1.5">
                      <span className="size-2 rounded-full bg-destructive" />
                      p99{" "}
                      <span className="font-mono text-foreground">
                        {reading(metrics, "serving.latency_p99")}
                      </span>
                    </span>
                    <span className="inline-flex items-center gap-1.5">
                      <span className="size-2 rounded-full bg-amber-500" />
                      p95{" "}
                      <span className="font-mono text-foreground">
                        {reading(metrics, "serving.latency_p95")}
                      </span>
                    </span>
                    <span className="inline-flex items-center gap-1.5">
                      <span className="size-2 rounded-full bg-primary" />
                      p50{" "}
                      <span className="font-mono text-foreground">
                        {reading(metrics, "serving.latency_p50")}
                      </span>
                    </span>
                  </div>
                  <div className="border-t pt-3">
                    <FieldGrid>
                      <Field label="Time to first token (p50)">
                        {reading(metrics, "serving.ttft_p50")}
                      </Field>
                      <Field label="Time to first token (p95)">
                        {reading(metrics, "serving.ttft_p95")}
                      </Field>
                      <Field label="Requests in flight">
                        {reading(metrics, "serving.requests_in_flight")}
                      </Field>
                      <Field label="Tokens/sec in · out">
                        {reading(metrics, "serving.input_tokens_per_second")} ·{" "}
                        {reading(metrics, "serving.output_tokens_per_second")}
                      </Field>
                    </FieldGrid>
                  </div>
                </>
              ) : (
                <EmptyNote>nothing is measuring this endpoint</EmptyNote>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <AvailabilityCard
            windowLabel={chartWindow.label}
            loading={live.isLoading}
            percent={availableNow?.value ?? null}
            historyLoading={fortnight.isLoading}
            history={availableFortnight?.value ?? null}
            days={days}
          />
          <SlaCard endpointId={endpointId} />
        </div>
      </div>
    </>
  )
}
