import { POLL } from "@/lib/poll"
import { useQuery } from "@tanstack/react-query"

import {
  fetchEndpointSla,
  fetchMetrics,
  fetchServingMetrics,
  type MetricsQuery,
} from "./api"
import type { MetricScope } from "./types"

/** Measurement goes stale on its own, so it refetches; inventory does not and does not. */
const REFRESH_MS = POLL.metrics

export function useMetrics(
  scope: MetricScope,
  ref: string | undefined,
  query: MetricsQuery = {}
) {
  return useQuery({
    queryKey: ["metrics", scope, ref, query],
    queryFn: () => fetchMetrics(scope, ref!, query),
    enabled: Boolean(ref),
    refetchInterval: REFRESH_MS,
    /**
     * One retry, not the default three. A metrics store that is down stays down
     * for longer than a panel is worth blocking on, and the plane already turns
     * that case into a body carrying `unavailable` instead of an error.
     */
    retry: 1,
    staleTime: REFRESH_MS / 2,
  })
}

/** The gateway's own totals, and every endpoint's figures beside them. */
export function useServingMetrics(query: MetricsQuery = {}) {
  return useQuery({
    queryKey: ["metrics", "serving", query],
    queryFn: () => fetchServingMetrics(query),
    refetchInterval: REFRESH_MS,
    retry: 1,
    staleTime: REFRESH_MS / 2,
  })
}

/**
 * An endpoint's SLA report for one calendar month. A month that has ended does
 * not move, so only the running one refetches.
 */
export function useEndpointSla(
  ref: string | undefined,
  month: string,
  running: boolean
) {
  return useQuery({
    queryKey: ["metrics", "endpoint-sla", ref, month],
    queryFn: () => fetchEndpointSla(ref!, month),
    enabled: Boolean(ref),
    refetchInterval: running ? REFRESH_MS * 10 : false,
    retry: 1,
    staleTime: running ? REFRESH_MS : 10 * 60_000,
  })
}
