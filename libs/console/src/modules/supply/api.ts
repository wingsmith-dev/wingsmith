import { api, type ListResponse } from "@/lib/api"
import type {
  Device,
  DeviceClass,
  Pool,
  PoolCreateRequest,
  PoolUpdateRequest,
  NodesSummary,
  SupplyNode,
  TopologyDomain,
} from "./types"

/**
 * Every filter name below is the plane's, not the console's. Litestar drops
 * query params it does not declare without complaining, so a filter spelled
 * `cluster_id` is not a filter that fails: it is a filter that silently
 * returns everything.
 */
export interface NodeFilters {
  cluster?: string
  pool?: string
  condition?: string
  ready?: boolean
  cordoned?: boolean
  search?: string
  label?: string[]
}

export interface DeviceFilters {
  cluster?: string
  node?: string
  pool?: string
  /** Sent as `class`; `device_class` is not a parameter the plane knows. */
  class?: string
  health?: string
  allocatable?: boolean
  topology_domain?: string
}

export interface DeviceClassFilters {
  cluster?: string
  architecture?: string
  /** Only classes the fleet actually holds. */
  present?: boolean
}

export interface TopologyFilters {
  cluster?: string
  node?: string
  kind?: string
  spans_nodes?: boolean
}

export interface PoolFilters {
  cluster?: string
  class?: string
  enabled?: boolean
  search?: string
  label?: string[]
}

export function fetchNodes(
  filters: NodeFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<SupplyNode>> {
  return api.list<SupplyNode>("/nodes", { ...filters, cursor, limit })
}

export function fetchNode(ref: string): Promise<SupplyNode> {
  return api.get<SupplyNode>(`/nodes/${encodeURIComponent(ref)}`)
}

/** Fleet-wide node rollup, computed server-side across every node. */
export function fetchNodesSummary(): Promise<NodesSummary> {
  return api.get<NodesSummary>("/nodes/summary")
}

export function fetchDevices(
  filters: DeviceFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Device>> {
  return api.list<Device>("/devices", { ...filters, cursor, limit })
}

export function fetchDevice(ref: string): Promise<Device> {
  return api.get<Device>(`/devices/${encodeURIComponent(ref)}`)
}

/**
 * The catalog is not paginated, it is bounded by the hardware that exists,
 * so this returns the array instead of a cursor page.
 */
export async function fetchDeviceClasses(
  filters: DeviceClassFilters = {}
): Promise<DeviceClass[]> {
  const result = await api.list<DeviceClass>("/device-classes", { ...filters })
  return result.items
}

export function fetchTopologyDomains(
  filters: TopologyFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<TopologyDomain>> {
  return api.list<TopologyDomain>("/topology-domains", {
    ...filters,
    cursor,
    limit,
  })
}

export function fetchPools(
  filters: PoolFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Pool>> {
  return api.list<Pool>("/pools", { ...filters, cursor, limit })
}

export function fetchPool(ref: string): Promise<Pool> {
  return api.get<Pool>(`/pools/${encodeURIComponent(ref)}`)
}

export function updatePool(
  ref: string,
  body: PoolUpdateRequest,
  revision?: string
): Promise<Pool> {
  return api.patch<Pool>(`/pools/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function createPool(body: PoolCreateRequest): Promise<Pool> {
  return api.post<Pool>("/pools", body)
}

export function deletePool(ref: string): Promise<void> {
  return api.delete<void>(`/pools/${encodeURIComponent(ref)}`)
}
