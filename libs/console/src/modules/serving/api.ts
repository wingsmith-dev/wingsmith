import { api } from "@/lib/api"
import type { MetricsQuery } from "@/modules/metrics/api"
import type { Metrics } from "@/modules/metrics/types"
import type { ServedModelDetail, ServingOverview } from "./types"

/**
 * A tenant session reads its own view and cannot name another: the plane takes
 * the tenant off the principal and ignores what is asked for. `tenant` is the
 * admin's way in — the same three reads, scoped to the customer being looked at,
 * so the console never grows a second rendering of what a tenant sees.
 */
export function fetchServingOverview(
  tenant?: string
): Promise<ServingOverview> {
  return api.get<ServingOverview>("/serving", { tenant })
}

export function fetchServedModel(
  ref: string,
  tenant?: string
): Promise<ServedModelDetail> {
  return api.get<ServedModelDetail>(
    `/serving/models/${encodeURIComponent(ref)}`,
    { tenant }
  )
}

export function fetchServedModelMetrics(
  ref: string,
  query: MetricsQuery = {},
  tenant?: string
): Promise<Metrics> {
  return api.get<Metrics>(
    `/serving/models/${encodeURIComponent(ref)}/metrics`,
    {
      window: query.window,
      step: query.step,
      span: query.span,
      series: query.series,
      tenant,
    }
  )
}
