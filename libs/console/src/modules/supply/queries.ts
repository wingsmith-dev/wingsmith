import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  createPool,
  deletePool,
  fetchDevice,
  fetchDeviceClasses,
  fetchDevices,
  fetchNode,
  fetchNodes,
  fetchNodesSummary,
  fetchPool,
  fetchPools,
  fetchTopologyDomains,
  updatePool,
  type DeviceClassFilters,
  type DeviceFilters,
  type NodeFilters,
  type PoolFilters,
  type TopologyFilters,
} from "./api"
import type { DeviceClass, PoolCreateRequest, PoolUpdateRequest } from "./types"

export function useNodes(filters: NodeFilters = {}) {
  return useCursorList(["nodes", filters], (cursor) =>
    fetchNodes(filters, cursor)
  )
}

/** Fleet-wide node rollup for the list header: true totals, not a page. */
export function useNodesSummary() {
  return useQuery({
    queryKey: ["nodes", "summary"],
    queryFn: () => fetchNodesSummary(),
  })
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

/**
 * The device catalog. Cached hard and for a long time: it changes when a
 * release changes the contract table, not when the fleet does anything, and
 * every select on every form reads it.
 */
export function useDeviceClasses(filters: DeviceClassFilters = {}) {
  return useQuery({
    queryKey: ["device-classes", filters],
    queryFn: () => fetchDeviceClasses(filters),
    staleTime: 5 * 60 * 1000,
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

/** Conditional on `revision`; a stale edit is refused, not merged. */
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

/**
 * The identifier of a class, and the name a person reads for it.
 *
 * `name` is what is stored, sent and filtered on; the label is display only and
 * is never round-tripped. Keeping them in one component is what stops a form
 * from posting "NVIDIA H100 80GB SXM" as a device class.
 */
export function useDeviceClassLabel(): (name: string) => string {
  const { data } = useDeviceClasses()
  return (name: string) =>
    data?.find((entry) => entry.name === name)?.label ?? name
}

/**
 * The options for a class picker, with `current` guaranteed present.
 *
 * A pool or a workload can carry a class the catalog does not list: hardware
 * nobody has named yet still resolves and still gets scheduled onto. Dropping
 * it from the list would mean opening the edit form and silently changing the
 * value, so it is always offered, marked as the unlisted thing it is.
 */
export function useDeviceClassOptions(current?: string) {
  const { data, isLoading } = useDeviceClasses()
  const classes = data ?? []

  const options = classes.map((entry) => ({
    value: entry.name,
    label: optionLabel(entry),
  }))

  if (current && !classes.some((entry) => entry.name === current)) {
    options.unshift({ value: current, label: `${current} · not in catalog` })
  }
  return { options, isLoading }
}

function optionLabel(entry: DeviceClass): string {
  const held = entry.inventory.devices
  const owned = held > 0 ? `${held} in fleet` : "none in fleet"
  return `${entry.label} · ${owned}`
}
