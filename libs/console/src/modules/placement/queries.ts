import { POLL } from "@/lib/poll"
import { useQuery } from "@tanstack/react-query"
import type { PlacementShapeRequest } from "./types"
import {
  expandWorkload,
  fetchReservations,
  previewPlacement,
  type ReservationFilters,
} from "./api"

/**
 * A decision is only true for as long as the fleet holds still, so it is cached
 * briefly and never held across a refresh of the board. Fetched on demand rather
 * than for every brick on mount: each call costs the plane a pass over quota,
 * policies and per-cluster capacity, and a tray of thirty bricks would ask
 * thirty times to fill a column nobody has looked at yet.
 */
const STALE_MS = 15_000

/**
 * The plane's verdict on a placement nobody has committed to.
 *
 * Either subject will do, and exactly one must be given: a `workload` that exists,
 * or a bare `shape` for a configuration that does not yet. The second is what a
 * create form has, and it is why this hook is worth having beside
 * `useWorkloadExpansion`: the answer arrives before the write, not after.
 *
 * A shape-form decision comes back under the workload id `wkl_preview`, which is a
 * deliberate non-uid: nothing was created, and a plausible-looking uid in a decision
 * that resolves to nothing is a 404 for whoever pastes it. Never render it as a link.
 */
export function usePlacementPreview(
  args: {
    workload?: string | null
    shape?: PlacementShapeRequest | null
    project: string | null
    priority_class?: string | null
    replicas: number
  },
  enabled: boolean
) {
  const { workload, shape, project, priority_class, replicas } = args
  const subject = workload ?? shape ?? null
  return useQuery({
    queryKey: [
      "placement",
      "preview",
      workload ?? null,
      shape ?? null,
      project,
      priority_class ?? null,
      replicas,
    ],
    queryFn: () =>
      previewPlacement({
        // Exactly one, never both: the plane rejects a request naming two subjects,
        // and a workload is the more specific answer when there is one.
        ...(workload ? { workload } : { shape: shape! }),
        project: project!,
        priority_class,
        replicas,
      }),
    enabled: enabled && !!subject && !!project,
    staleTime: STALE_MS,
    retry: false,
  })
}

/**
 * Reservations decay as ingestion reports the allocations that satisfy them, so
 * they are polled on the same cadence the board polls allocations. Any slower
 * and the two numbers on one card would be read off different moments: a
 * reservation that has already been bound, sitting beside the devices that bound
 * it, reads as double the demand there actually is.
 */
const RESERVATIONS_POLL_MS = POLL.allocations

export function useReservations(filters: ReservationFilters = {}) {
  return useQuery({
    queryKey: [
      "placement",
      "reservations",
      filters.cluster ?? null,
      filters.device_class ?? null,
    ],
    queryFn: () => fetchReservations(filters),
    refetchInterval: RESERVATIONS_POLL_MS,
  })
}

export function useWorkloadExpansion(
  workload: string | null,
  enabled: boolean
) {
  return useQuery({
    queryKey: ["placement", "expand", workload],
    queryFn: () => expandWorkload(workload!),
    enabled: enabled && !!workload,
    staleTime: STALE_MS,
    retry: false,
  })
}
