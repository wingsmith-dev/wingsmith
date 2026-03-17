import { api, type ListResponse } from "@/lib/api"
import type { Device, NodeDetail, Pool, SupplyNode, TopologyDomain } from "./types"

export interface NodeFilters {
  cluster_id?: string
  pool_id?: string
  condition?: string
  cordoned?: boolean
}

export interface DeviceFilters {
  cluster_id?: string
  node_id?: string
  topology_domain_id?: string
  device_class?: string
  health?: string
  allocated?: boolean
}

export function fetchNodes(filters: NodeFilters = {}) {
  return api.get<ListResponse<SupplyNode>>("/nodes", { ...filters })
}

export async function fetchNode(nodeId: string) {
  const data = await api.get<{ node: NodeDetail }>(`/nodes/${nodeId}`)
  return data.node
}

export function fetchNodeDevices(nodeId: string) {
  return api.get<ListResponse<Device>>(`/nodes/${nodeId}/devices`)
}

export function fetchDevices(filters: DeviceFilters = {}) {
  return api.get<ListResponse<Device>>("/devices", { ...filters })
}

export async function fetchDevice(deviceId: string) {
  const data = await api.get<{ device: Device }>(`/devices/${deviceId}`)
  return data.device
}

export function fetchTopologyDomains(clusterId?: string) {
  return api.get<ListResponse<TopologyDomain>>("/topology-domains", {
    cluster_id: clusterId,
  })
}

export function fetchPools(clusterId?: string) {
  return api.get<ListResponse<Pool>>("/pools", { cluster_id: clusterId })
}

export async function fetchPool(poolId: string) {
  const data = await api.get<{ pool: Pool }>(`/pools/${poolId}`)
  return data.pool
}

export function fetchPoolNodes(poolId: string) {
  return api.get<ListResponse<SupplyNode>>(`/pools/${poolId}/nodes`)
}
