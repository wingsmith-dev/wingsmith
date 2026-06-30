import { useQuery } from "@tanstack/react-query"

import { windowQuery, type TimeWindow } from "@/modules/metrics/windows"

import {
  fetchServedModel,
  fetchServedModelMetrics,
  fetchServingOverview,
} from "./api"

/** How often the view re-reads. Slow: this is a status page, not a dashboard. */
const REFETCH_MS = 30_000

export function useServingOverview() {
  return useQuery({
    queryKey: ["serving", "overview"],
    queryFn: fetchServingOverview,
    refetchInterval: REFETCH_MS,
  })
}

export function useServedModel(ref: string | null) {
  return useQuery({
    queryKey: ["serving", "model", ref],
    queryFn: () => fetchServedModel(ref as string),
    enabled: ref !== null,
  })
}

/**
 * One model's traffic over a chosen span, with the curves asked for.
 *
 * `placeholderData` keeps the previous render on screen while a new window
 * loads, so changing the range redraws rather than collapsing the page to
 * skeletons and jumping the layout back when the answer arrives.
 */
export function useServedModelMetrics(ref: string | null, window: TimeWindow) {
  return useQuery({
    queryKey: ["serving", "metrics", ref, window.key],
    queryFn: () => fetchServedModelMetrics(ref as string, windowQuery(window)),
    enabled: ref !== null,
    refetchInterval: REFETCH_MS,
    placeholderData: (previous) => previous,
  })
}
