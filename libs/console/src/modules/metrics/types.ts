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
