import { useQuery } from "@tanstack/react-query"
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
 * policies and per-cluster capacity, and a tray of thirty recipes would ask
 * thirty times to fill a column nobody has looked at yet.
 */
const STALE_MS = 15_000

export function usePlacementPreview(
  args: {
    recipe: string | null
    project: string | null
    priority_class?: string | null
    replicas: number
  },
  enabled: boolean
) {
  const { recipe, project, priority_class, replicas } = args
  return useQuery({
    queryKey: [
      "placement",
      "preview",
      recipe,
      project,
      priority_class ?? null,
      replicas,
    ],
    queryFn: () =>
      previewPlacement({
        recipe: recipe!,
        project: project!,
        priority_class,
        replicas,
      }),
    enabled: enabled && !!recipe && !!project,
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
const RESERVATIONS_POLL_MS = 10_000

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
