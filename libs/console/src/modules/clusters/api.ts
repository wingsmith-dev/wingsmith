import { api, type ListResponse } from "@/lib/api"
import type {
  Cluster,
  ClusterCreateRequest,
  ClusterServingRequest,
  ClusterUpdateRequest,
} from "./types"

export interface ClusterFilters {
  region?: string
  phase?: string
  search?: string
  /** Repeatable `label=k=v`; the plane rejects anything that is not `k=v`. */
  label?: string[]
}

export function fetchClusters(
  filters: ClusterFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Cluster>> {
  return api.list<Cluster>("/clusters", { ...filters, cursor, limit })
}

export function createCluster(body: ClusterCreateRequest): Promise<Cluster> {
  return api.post<Cluster>("/clusters", body)
}

/** Resolves by id or by name — `libs/packages/contract/API.md` §3 accepts either. */
export function fetchCluster(ref: string): Promise<Cluster> {
  return api.get<Cluster>(`/clusters/${encodeURIComponent(ref)}`)
}

/** Conditional on `revision`; a mismatch is `409 conflict`. */
export function updateCluster(
  ref: string,
  body: ClusterUpdateRequest,
  revision?: string
): Promise<Cluster> {
  return api.patch<Cluster>(`/clusters/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

/** A full replacement — the plane has no partial update for serving config. */
export function setClusterServing(
  ref: string,
  body: ClusterServingRequest
): Promise<Cluster> {
  return api.put<Cluster>(`/clusters/${encodeURIComponent(ref)}/serving`, body)
}

export function clearClusterServing(ref: string): Promise<void> {
  return api.delete<void>(`/clusters/${encodeURIComponent(ref)}/serving`)
}

export function deleteCluster(ref: string): Promise<void> {
  return api.delete<void>(`/clusters/${encodeURIComponent(ref)}`)
}
