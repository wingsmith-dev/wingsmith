import { api, type ListResponse } from "@/lib/api"
import type { Deployment } from "@/modules/demand/types"
import type {
  DecisionRequest,
  DiscoveredWorkload,
  PromoteRequest,
  ServingDraft,
} from "./types"

export interface DiscoveredFilters {
  cluster?: string
  namespace?: string
  status?: string
  search?: string
}

export function fetchDiscoveredWorkloads(
  filters: DiscoveredFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<DiscoveredWorkload>> {
  return api.list<DiscoveredWorkload>("/discovered", {
    ...filters,
    cursor,
    limit,
  })
}

export function fetchDiscoveredWorkload(
  ref: string,
  cluster?: string
): Promise<DiscoveredWorkload> {
  return api.get<DiscoveredWorkload>(
    `/discovered/${encodeURIComponent(ref)}`,
    cluster ? { cluster } : undefined
  )
}

export function trackDiscoveredWorkload(
  ref: string,
  body: DecisionRequest = {}
): Promise<DiscoveredWorkload> {
  return api.post<DiscoveredWorkload>(
    `/discovered/${encodeURIComponent(ref)}/track`,
    body
  )
}

export function ignoreDiscoveredWorkload(
  ref: string,
  body: DecisionRequest = {}
): Promise<DiscoveredWorkload> {
  return api.post<DiscoveredWorkload>(
    `/discovered/${encodeURIComponent(ref)}/ignore`,
    body
  )
}

/**
 * Takes a sighting off the list for good. A DELETE even though nothing observed
 * is deleted: what goes is the entry, the sighting under it belongs to ingestion,
 * and `restoreDiscoveredWorkload` puts it back.
 */
export function dismissDiscoveredWorkload(ref: string): Promise<void> {
  return api.delete<void>(`/discovered/${encodeURIComponent(ref)}`)
}

export function restoreDiscoveredWorkload(
  ref: string
): Promise<DiscoveredWorkload> {
  return api.post<DiscoveredWorkload>(
    `/discovered/${encodeURIComponent(ref)}/restore`,
    {}
  )
}

/**
 * The only call here that produces declared state. What comes back is a
 * deployment, and from that point the object is managed through `/v1/deployments`
 * like any other.
 */
export function promoteDiscoveredWorkload(
  ref: string,
  body: PromoteRequest
): Promise<Deployment> {
  return api.post<Deployment>(
    `/discovered/${encodeURIComponent(ref)}/promote`,
    body
  )
}

/**
 * What the plane reads this sighting as. A GET because it is one: the same sighting
 * derives the same draft every time, and asking does not change anything.
 */
export function fetchServingDraft(
  ref: string,
  cluster?: string
): Promise<ServingDraft> {
  return api.get<ServingDraft>(
    `/discovered/${encodeURIComponent(ref)}/serving-draft`,
    cluster ? { cluster } : undefined
  )
}
