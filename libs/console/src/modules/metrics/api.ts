import { api } from "@/lib/api"

import type { Metrics, MetricScope } from "./types"

const PATHS: Record<MetricScope, string> = {
  cluster: "clusters",
  pool: "pools",
  node: "nodes",
  device: "devices",
  deployment: "deployments",
  workload: "workloads",
  endpoint: "endpoints",
  tenant: "tenants",
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
  return api.get<Metrics>(`/metrics/${PATHS[scope]}/${ref}`, {
    window: query.window,
    series: query.series,
    span: query.span,
    step: query.step,
  })
}

/**
 * Everything the gateway answered, with one breakdown row per endpoint.
 *
 * One query for a whole page of endpoints, rather than one per row: the total is
 * the front door's own and does not move when somebody pages, and a table of
 * fifty names must not be fifty round trips to a Prometheus.
 */
export function fetchServingMetrics(query: MetricsQuery = {}) {
  return api.get<Metrics>("/metrics/serving", {
    window: query.window,
    series: query.series,
    span: query.span,
    step: query.step,
  })
}
