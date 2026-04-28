import { api } from "@/lib/api"
import type {
  ExpansionDecision,
  PlacementDecision,
  PlacementPreviewRequest,
} from "./types"

/**
 * Both routes are `POST` and neither writes anything.
 *
 * That is not a mistake in the contract: the question carries a body — a recipe,
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
