import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  clearClusterServing,
  createCluster,
  deleteCluster,
  fetchCluster,
  fetchClusters,
  mintClusterInstall,
  setClusterServing,
  updateCluster,
  type ClusterFilters,
} from "./api"
import type {
  ClusterCreateRequest,
  ClusterServingRequest,
  ClusterUpdateRequest,
} from "./types"

export function useClusters(filters: ClusterFilters = {}) {
  return useCursorList(["clusters", filters], (cursor) =>
    fetchClusters(filters, cursor)
  )
}

/** Every cluster, drained across pages — for filter controls and name lookups. */
export function useClusterOptions() {
  return useAllPages(["clusters", "options"], (cursor) =>
    fetchClusters({}, cursor, 200)
  )
}

export function useCluster(ref: string) {
  return useQuery({
    queryKey: ["clusters", "detail", ref],
    queryFn: () => fetchCluster(ref),
    enabled: !!ref,
  })
}

export function useCreateCluster() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: ClusterCreateRequest) => createCluster(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["clusters"] }),
  })
}

/**
 * Carries the cluster's `revision` as `If-Match`, so an edit written against a
 * stale read is rejected by the plane rather than silently overwriting it.
 */
export function useUpdateCluster() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: ClusterUpdateRequest
      revision?: string
    }) => updateCluster(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["clusters"] }),
  })
}

export function useSetClusterServing() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: { ref: string; body: ClusterServingRequest }) =>
      setClusterServing(vars.ref, vars.body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["clusters"] }),
  })
}

export function useClearClusterServing() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => clearClusterServing(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["clusters"] }),
  })
}

/**
 * Mints an install one-liner on demand. A mutation, not a query: each call
 * returns a fresh short-lived token, so it is never cached or refetched.
 */
export function useClusterInstall() {
  return useMutation({
    mutationFn: (ref: string) => mintClusterInstall(ref),
  })
}

export function useDeleteCluster() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteCluster(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["clusters"] })
      client.invalidateQueries({ queryKey: ["pools"] })
    },
  })
}
