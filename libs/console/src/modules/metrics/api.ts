import { api } from "@/lib/api"

import type { Metrics, MetricScope } from "./types"

const PATHS: Record<MetricScope, string> = {
  cluster: "clusters",
  pool: "pools",
  node: "nodes",
  device: "devices",
  deployment: "deployments",
}

export interface MetricsQuery {
  /** Rate and quantile window, e.g. `5m`. The plane's default applies when unset. */
  window?: string
  /** Ask for time series as well as instant values. Costs a `query_range` per metric. */
  series?: boolean
  /** How far back the series reaches, in seconds. */
  span?: number
  step?: string
}

export function fetchMetrics(
  scope: MetricScope,
  ref: string,
  query: MetricsQuery = {}
) {
  return api.get<Metrics>(`/v1/metrics/${PATHS[scope]}/${ref}`, {
    window: query.window,
    series: query.series,
    span: query.span,
    step: query.step,
  })
}
