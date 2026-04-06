import { useQuery } from "@tanstack/react-query"

import { fetchMetrics, type MetricsQuery } from "./api"
import type { MetricScope } from "./types"

/** Measurement goes stale on its own, so it refetches; inventory does not and does not. */
const REFRESH_MS = 30_000

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
     * that case into a body carrying `unavailable` rather than an error.
     */
    retry: 1,
    staleTime: REFRESH_MS / 2,
  })
}
