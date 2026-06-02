import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useCursorList } from "@/lib/paginate"
import {
  fetchDiscoveredWorkload,
  fetchServingDraft,
  fetchDiscoveredWorkloads,
  dismissDiscoveredWorkload,
  ignoreDiscoveredWorkload,
  restoreDiscoveredWorkload,
  promoteDiscoveredWorkload,
  trackDiscoveredWorkload,
  type DiscoveredFilters,
} from "./api"
import type { DecisionRequest, PromoteRequest } from "./types"

export function useDiscoveredWorkloads(filters: DiscoveredFilters = {}) {
  return useCursorList(["discovered", filters], (cursor) =>
    fetchDiscoveredWorkloads(filters, cursor)
  )
}

export function useDiscoveredWorkload(ref: string, cluster?: string) {
  return useQuery({
    queryKey: ["discovered", "detail", ref, cluster],
    queryFn: () => fetchDiscoveredWorkload(ref, cluster),
    enabled: !!ref,
  })
}

/**
 * How many sightings nobody has looked at yet. Its own small query so the
 * sidebar can carry the count without the list page being mounted.
 */
export function useUndecidedCount() {
  const query = useQuery({
    queryKey: ["discovered", "undecided"],
    queryFn: () => fetchDiscoveredWorkloads({ status: "new" }, undefined, 100),
  })
  return query.data?.items.length ?? 0
}

function useDecision(
  fn: (ref: string, body: DecisionRequest) => Promise<unknown>
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ ref, body }: { ref: string; body?: DecisionRequest }) =>
      fn(ref, body ?? {}),
    onSuccess: () => client.invalidateQueries({ queryKey: ["discovered"] }),
  })
}

export function useTrackWorkload() {
  return useDecision(trackDiscoveredWorkload)
}

export function useIgnoreWorkload() {
  return useDecision(ignoreDiscoveredWorkload)
}

/**
 * Removing a sighting from the list. Nothing reaches a cluster and nothing
 * observed is deleted — the workload goes on running, and `useRestoreWorkload`
 * brings the entry back.
 */
export function useDismissWorkload() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ ref }: { ref: string }) => dismissDiscoveredWorkload(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["discovered"] }),
  })
}

export function useRestoreWorkload() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ ref }: { ref: string }) => restoreDiscoveredWorkload(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["discovered"] }),
  })
}

/**
 * Invalidates deployments as well as sightings: a promotion creates one, and
 * the catalogue is wrong until it is re-read.
 */
export function usePromoteWorkload() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ ref, body }: { ref: string; body: PromoteRequest }) =>
      promoteDiscoveredWorkload(ref, body),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["discovered"] })
      client.invalidateQueries({ queryKey: ["deployments"] })
    },
  })
}

/** The proposed serving block for one sighting, fetched when its dialog opens. */
export function useServingDraft(ref: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ["discovered", "serving-draft", ref],
    queryFn: () => fetchServingDraft(ref!),
    enabled: !!ref && enabled,
  })
}
