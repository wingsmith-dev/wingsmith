import { useQuery } from "@tanstack/react-query"

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

export function useServedModelMetrics(ref: string | null) {
  return useQuery({
    queryKey: ["serving", "metrics", ref],
    queryFn: () => fetchServedModelMetrics(ref as string),
    enabled: ref !== null,
    refetchInterval: REFETCH_MS,
  })
}
