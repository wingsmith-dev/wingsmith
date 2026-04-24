import { useQuery } from "@tanstack/react-query"

import { fetchPlaneConfig } from "./api"

/**
 * The plane's effective configuration. Cached for a while: it only changes when
 * the process is redeployed, so polling it would be noise — but not `Infinity`,
 * because a deploy during a long-lived session should eventually show through.
 */
const STALE_MS = 60_000

export function usePlaneConfig() {
  return useQuery({
    queryKey: ["system", "config"],
    queryFn: fetchPlaneConfig,
    staleTime: STALE_MS,
  })
}
