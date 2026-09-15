/** Mirrors `app/modules/metrics/schemas.py`. Measurement, never inventory. */

/**
 * `endpoint` is the one scope that is not a cluster or a part of one. It is
 * answered from the gateway's own series, so it is the only scope whose answer
 * carries no cluster at all.
 */
export type MetricScope =
  | "cluster"
  | "pool"
  | "node"
  | "device"
  | "deployment"
  | "workload"
  | "endpoint"
  | "tenant"

export type MetricUnit =
  | "percent"
  | "celsius"
  | "watts"
  | "mib"
  | "mhz"
  | "seconds"
  | "per_second"
  | "count"
  | (string & {})

export interface MetricSource {
  cluster: string
  url: string
  kind: string
}

export interface MetricSample {
  metric: string
  title: string
  unit: MetricUnit
  /** `null` is "the store had no series", which is not zero and must not render as zero. */
  value: number | null
}

export interface MetricSeriesPoint {
  at: number
  value: number
}

export interface MetricSeries {
  metric: string
  title: string
  unit: MetricUnit
  labels: Record<string, string>
  points: MetricSeriesPoint[]
}

export interface MetricBreakdown {
  key: string
  label: string
  samples: MetricSample[]
}

export interface Metrics {
  scope: MetricScope
  ref: string
  cluster: string | null
  source: MetricSource | null
  window: string
  samples: MetricSample[]
  breakdown: MetricBreakdown[]
  series: MetricSeries[]
  /** Why the panel is empty, when it is. Absence with a reason beats a zero. */
  unavailable: string | null
}

/** One latency at the three percentiles a contract quotes, in seconds. */
export interface SlaPercentiles {
  p50: number | null
  p95: number | null
  p99: number | null
}

/** The commitment quoted from the endpoint's serving quota. */
export interface SlaObjective {
  /** A fraction in (0, 1], never a percentage. `null` when the quota commits to none. */
  success_rate: number | null
  window_seconds: number
  enforced: boolean
}

/**
 * Mirrors `SlaReportSchema`: one endpoint's service level over a period.
 *
 * `availability` and `failure_ratio` are percentages on 0-100. `met` and
 * `error_budget_consumed` are `null` whenever the reading or the commitment
 * they need is missing — never a `false` that means "not measured".
 */
export interface SlaReport {
  endpoint: string
  endpoint_name: string
  period_start: string
  period_end: string
  /** The period is still running and the figures stop at `period_end`, which is now. */
  partial: boolean
  definition: string
  definition_text: string
  requests: number | null
  availability: number | null
  failure_ratio: number | null
  latency: SlaPercentiles
  ttft: SlaPercentiles
  objective: SlaObjective | null
  met: boolean | null
  /** Failure spent over failure allowed: 1 is the whole budget. */
  error_budget_consumed: number | null
  unavailable: string | null
}
