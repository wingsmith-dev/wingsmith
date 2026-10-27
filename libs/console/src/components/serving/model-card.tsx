import { Card } from "@/components/ui/card"
import { StatusDot, type StatusTone } from "@/components/layout/status-dot"
import { formatMetric, valueOf } from "@/modules/metrics/format"
import {
  useServedModel,
  useServedModelMetrics,
} from "@/modules/serving/queries"
import type { TimeWindow } from "@/modules/metrics/windows"
import type { ServedModel, ServedModelState } from "@/modules/serving/types"

import { ServingCharts } from "./serving-charts"

const TONE: Record<ServedModelState, StatusTone> = {
  serving: "ok",
  degraded: "danger",
  paused: "neutral",
}

const LABEL: Record<ServedModelState, string> = {
  serving: "Serving",
  degraded: "Not answering",
  paused: "Paused",
}

/**
 * What the state means in the reader's terms, not ours.
 *
 * `degraded` says nothing is answering right now, which is the only accurate thing
 * to say from here: whether that is a restart, a placement, or an incident is a
 * question about clusters, and clusters are not this reader's to see.
 */
const EXPLAIN: Record<ServedModelState, string> = {
  serving: "Requests are being answered.",
  degraded: "Nothing is answering this model right now.",
  paused: "Turned off. It will answer again when it is turned back on.",
}

interface StatProps {
  label: string
  value: string
}

function Stat({ label, value }: StatProps) {
  return (
    <div className="min-w-0">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="truncate font-mono text-lg tabular-nums">{value}</p>
    </div>
  )
}

/**
 * One served model: the name a client sends, whether it answers, and the traffic
 * it has been carrying.
 *
 * The numbers come from the gateway's own series and are absent, never zero,
 * when the store has nothing. A model nobody called this hour did not serve zero
 * requests at zero latency; it was not measured, and a dash says so.
 *
 * `tenant` is absent on the tenant's own page and names the customer when an
 * admin is looking at one. Nothing else differs between the two readers: an
 * admin inspecting a tenant should see the page that tenant sees, not a second
 * rendering of it that can drift from what the customer is looking at.
 */
export function ModelCard({
  model,
  window,
  tenant,
}: {
  model: ServedModel
  window: TimeWindow
  tenant?: string
}) {
  const detail = useServedModel(model.id, tenant)
  const metrics = useServedModelMetrics(model.id, window, tenant)
  const samples = metrics.data?.samples ?? []

  const stat = (metric: string) => {
    const value = valueOf(samples, metric)
    if (value === null) return "—"
    const unit =
      samples.find((sample) => sample.metric === metric)?.unit ?? "count"
    return formatMetric(value, unit)
  }

  const limits = detail.data?.limits ?? null

  return (
    <Card className="gap-0 p-0">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b p-5">
        <div className="min-w-0">
          <p className="truncate font-mono text-base font-medium">
            {model.model}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {EXPLAIN[model.state]}
          </p>
        </div>
        <div className="flex items-center gap-2 whitespace-nowrap">
          <StatusDot tone={TONE[model.state]} />
          <span className="text-sm">{LABEL[model.state]}</span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-5 p-5 sm:grid-cols-3 lg:grid-cols-6">
        <Stat
          label="Requests / sec"
          value={stat("serving.requests_per_second")}
        />
        <Stat
          label="Output tokens / sec"
          value={stat("serving.output_tokens_per_second")}
        />
        <Stat label="TTFT p50" value={stat("serving.ttft_p50")} />
        <Stat label="Latency p95" value={stat("serving.latency_p95")} />
        <Stat label="Cached input" value={stat("serving.cache_hit_ratio")} />
        <Stat label="Availability" value={stat("serving.availability")} />
      </div>

      <ServingCharts curves={metrics.data?.series ?? []} />

      {limits ? (
        <div className="flex flex-wrap gap-x-6 gap-y-1 border-t px-5 py-3 text-xs text-muted-foreground">
          {limits.requests_per_minute !== null ? (
            <span>
              {limits.requests_per_minute.toLocaleString()} requests / min
            </span>
          ) : null}
          {limits.tokens_per_minute !== null ? (
            <span>
              {limits.tokens_per_minute.toLocaleString()} tokens / min
            </span>
          ) : null}
          {limits.max_concurrent_requests !== null ? (
            <span>
              {limits.max_concurrent_requests.toLocaleString()} concurrent
            </span>
          ) : null}
          {!limits.enforced ? <span>Not enforced</span> : null}
        </div>
      ) : null}
    </Card>
  )
}
