import { api } from "@/lib/api"
import type {
  ExpansionDecision,
  PlacementDecision,
  PlacementPreviewRequest,
  Reservation,
} from "./types"

/**
 * Both routes are `POST` and neither writes anything.
 *
 * That is not a mistake in the contract: the question carries a body — a workload,
 * a project, a priority class, a scale — and none of that fits a GET's cache
 * semantics when the answer changes every time a cluster reports.
 */
export function previewPlacement(
  body: PlacementPreviewRequest
): Promise<PlacementDecision> {
  return api.query<PlacementDecision>("/placement/preview", body)
}

export function expandWorkload(workload: string): Promise<ExpansionDecision> {
  return api.query<ExpansionDecision>("/placement/expand", { workload })
}

export interface ReservationFilters {
  cluster?: string
  device_class?: string
}

/**
 * Every promise the fleet is carrying, in one unpaginated array.
 *
 * No cursor, and that is the route's shape rather than an oversight here: a
 * reservation lives only as long as a cluster takes to report what it bound, so
 * the collection is bounded by what is converging right now. Draining it is the
 * same reasoning the board applies to everything it counts — a reserved total
 * answered over the first page is not a smaller answer, it is a wrong one.
 */
export function fetchReservations(
  filters: ReservationFilters = {}
): Promise<Reservation[]> {
  return api.get<Reservation[]>("/placement/reservations", { ...filters })
}
