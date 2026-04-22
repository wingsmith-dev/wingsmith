import { api, type ListResponse } from "@/lib/api"
import type {
  Cluster,
  ClusterCreateRequest,
  ClusterHeadroom,
  ClusterRollup,
  ClusterServingRequest,
  ClusterSetup,
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

/** Observed footprint per cluster in aggregate — for the list and card views. */
export function fetchClusterRollups(
  cursor?: string,
  limit?: number
): Promise<ListResponse<ClusterRollup>> {
  return api.list<ClusterRollup>("/clusters/rollups", { cursor, limit })
}

/** The same rollup, scoped to one cluster by id or name. */
export function fetchClusterRollup(ref: string): Promise<ClusterRollup> {
  return api.get<ClusterRollup>(
    `/clusters/${encodeURIComponent(ref)}/rollup`
  )
}

/** Free capacity and how it is shaped, for one cluster. */
export function fetchClusterHeadroom(ref: string): Promise<ClusterHeadroom> {
  return api.get<ClusterHeadroom>(
    `/clusters/${encodeURIComponent(ref)}/headroom`
  )
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

/** The `helm` install of the cluster-setup chart, with a per-cluster values block. */
export function fetchClusterSetup(ref: string): Promise<ClusterSetup> {
  return api.get<ClusterSetup>(`/clusters/${encodeURIComponent(ref)}/setup`)
}
