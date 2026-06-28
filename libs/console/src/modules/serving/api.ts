import { api } from "@/lib/api"
import type { Metrics } from "@/modules/metrics/types"
import type { ServedModelDetail, ServingOverview } from "./types"

/**
 * Every read here is scoped by the session, never by an argument. The plane takes
 * the tenant off the principal, so there is no tenant to pass and no way for this
 * client to ask for somebody else's.
 */
export function fetchServingOverview(): Promise<ServingOverview> {
  return api.get<ServingOverview>("/serving")
}

export function fetchServedModel(ref: string): Promise<ServedModelDetail> {
  return api.get<ServedModelDetail>(
    `/serving/models/${encodeURIComponent(ref)}`
  )
}

export function fetchServedModelMetrics(
  ref: string,
  span = 3600
): Promise<Metrics> {
  return api.get<Metrics>(
    `/serving/models/${encodeURIComponent(ref)}/metrics`,
    { span }
  )
}
