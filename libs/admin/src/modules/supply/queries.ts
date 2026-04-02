import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  createPool,
  deletePool,
  fetchDevice,
  fetchDevices,
  fetchNode,
  fetchNodes,
  fetchPool,
  fetchPools,
  fetchTopologyDomains,
  updatePool,
  type DeviceFilters,
  type NodeFilters,
  type PoolFilters,
  type TopologyFilters,
} from "./api"
import type { PoolCreateRequest, PoolUpdateRequest } from "./types"

export function useNodes(filters: NodeFilters = {}) {
  return useCursorList(["nodes", filters], (cursor) =>
    fetchNodes(filters, cursor)
  )
}

export function useNode(ref: string) {
  return useQuery({
    queryKey: ["nodes", "detail", ref],
    queryFn: () => fetchNode(ref),
    enabled: !!ref,
  })
}

export function useDevices(filters: DeviceFilters = {}) {
  return useCursorList(["devices", filters], (cursor) =>
    fetchDevices(filters, cursor)
  )
}

export function useDevice(ref: string) {
  return useQuery({
    queryKey: ["devices", "detail", ref],
    queryFn: () => fetchDevice(ref),
    enabled: !!ref,
  })
}

export function useTopologyDomains(filters: TopologyFilters = {}) {
  return useCursorList(["topology-domains", filters], (cursor) =>
    fetchTopologyDomains(filters, cursor)
  )
}

/**
 * Topology domains for one scope, drained. The gang-availability summary is an
 * aggregate over the whole set, so a first page would answer a different
 * question from the one it appears to answer.
 */
export function useAllTopologyDomains(filters: TopologyFilters = {}) {
  return useAllPages(["topology-domains", filters], (cursor) =>
    fetchTopologyDomains(filters, cursor, 200)
  )
}

export function usePools(filters: PoolFilters = {}) {
  return useCursorList(["pools", filters], (cursor) =>
    fetchPools(filters, cursor)
  )
}

export function usePool(ref: string) {
  return useQuery({
    queryKey: ["pools", "detail", ref],
    queryFn: () => fetchPool(ref),
    enabled: !!ref,
  })
}

export function useCreatePool() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: PoolCreateRequest) => createPool(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["pools"] }),
  })
}

/** Conditional on `revision`; a stale edit is refused rather than merged. */
export function useUpdatePool() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: PoolUpdateRequest
      revision?: string
    }) => updatePool(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["pools"] }),
  })
}

export function useDeletePool() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deletePool(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["pools"] }),
  })
}
