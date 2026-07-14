import { api } from "@/lib/api"
import type { MetricsQuery } from "@/modules/metrics/api"
import type { Metrics } from "@/modules/metrics/types"
import type { ServedModelDetail, ServingOverview } from "./types"

/**
 * Which tenant is in the path, never in a parameter.
 *
 * A tenant session reads `/serving` and the plane scopes it by the principal —
 * there is nothing to pass and nothing that could name somebody else. An admin
 * reads the same three views of a customer under `/tenants/{ref}/serving`,
 * which is a surface a tenant credential cannot reach at all.
 *
 * These are the same reads either way, so the console never grows a second
 * rendering of what a tenant sees.
 */
function servingBase(tenant?: string): string {
  return tenant ? `/tenants/${encodeURIComponent(tenant)}/serving` : "/serving"
}

export function fetchServingOverview(
  tenant?: string
): Promise<ServingOverview> {
  return api.get<ServingOverview>(servingBase(tenant))
}

export function fetchServedModel(
  ref: string,
  tenant?: string
): Promise<ServedModelDetail> {
  return api.get<ServedModelDetail>(
    `${servingBase(tenant)}/models/${encodeURIComponent(ref)}`
  )
}

export function fetchServedModelMetrics(
  ref: string,
  query: MetricsQuery = {},
  tenant?: string
): Promise<Metrics> {
  return api.get<Metrics>(
    `${servingBase(tenant)}/models/${encodeURIComponent(ref)}/metrics`,
    {
      window: query.window,
      step: query.step,
      span: query.span,
      series: query.series,
    }
  )
}
