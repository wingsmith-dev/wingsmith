import { POLL } from "@/lib/poll"
import { useQuery } from "@tanstack/react-query"

import { windowQuery, type TimeWindow } from "@/modules/metrics/windows"

import {
  fetchServedModel,
  fetchServedModelMetrics,
  fetchServingOverview,
} from "./api"

/** How often the view re-reads. Slow: this is a status page, not a dashboard. */
const REFETCH_MS = POLL.metrics

/**
 * `tenant` is undefined on the tenant's own page, the plane scopes that read by
 * the session, and is the tenant being inspected when an admin opens one, where
 * it selects the `/tenants/{ref}/serving` path instead of a parameter. It is
 * in the key either way, so an admin walking two tenants does not read the first
 * one's models out of the cache under the second one's name.
 */
export function useServingOverview(tenant?: string) {
  return useQuery({
    queryKey: ["serving", "overview", tenant ?? null],
    queryFn: () => fetchServingOverview(tenant),
    refetchInterval: REFETCH_MS,
  })
}

export function useServedModel(ref: string | null, tenant?: string) {
  return useQuery({
    queryKey: ["serving", "model", tenant ?? null, ref],
    queryFn: () => fetchServedModel(ref as string, tenant),
    enabled: ref !== null,
  })
}

/**
 * One model's traffic over a chosen span, with the curves asked for.
 *
 * `placeholderData` keeps the previous render on screen while a new window
 * loads, so changing the range redraws instead of collapsing the page to
 * skeletons and jumping the layout back when the answer arrives.
 */
export function useServedModelMetrics(
  ref: string | null,
  window: TimeWindow,
  tenant?: string
) {
  return useQuery({
    queryKey: ["serving", "metrics", tenant ?? null, ref, window.key],
    queryFn: () =>
      fetchServedModelMetrics(ref as string, windowQuery(window), tenant),
    enabled: ref !== null,
    refetchInterval: REFETCH_MS,
    placeholderData: (previous) => previous,
  })
}
