import { useQuery } from "@tanstack/react-query"
import {
  fetchDevice,
  fetchDevices,
  fetchNode,
  fetchNodeDevices,
  fetchNodes,
  fetchPool,
  fetchPoolNodes,
  fetchPools,
  fetchTopologyDomains,
  type DeviceFilters,
  type NodeFilters,
} from "./api"

export function useNodes(filters: NodeFilters = {}) {
  return useQuery({
    queryKey: ["nodes", filters],
    queryFn: () => fetchNodes(filters),
  })
}

export function useNode(nodeId: string) {
  return useQuery({
    queryKey: ["nodes", nodeId],
    queryFn: () => fetchNode(nodeId),
    enabled: !!nodeId,
  })
}

export function useNodeDevices(nodeId: string) {
  return useQuery({
    queryKey: ["nodes", nodeId, "devices"],
    queryFn: () => fetchNodeDevices(nodeId),
    enabled: !!nodeId,
  })
}

export function useDevices(filters: DeviceFilters = {}) {
  return useQuery({
    queryKey: ["devices", filters],
    queryFn: () => fetchDevices(filters),
  })
}

export function useDevice(deviceId: string) {
  return useQuery({
    queryKey: ["devices", deviceId],
    queryFn: () => fetchDevice(deviceId),
    enabled: !!deviceId,
  })
}

export function useTopologyDomains(clusterId?: string) {
  return useQuery({
    queryKey: ["topology-domains", { clusterId }],
    queryFn: () => fetchTopologyDomains(clusterId),
  })
}

export function usePools(clusterId?: string) {
  return useQuery({
    queryKey: ["pools", { clusterId }],
    queryFn: () => fetchPools(clusterId),
  })
}

export function usePool(poolId: string) {
  return useQuery({
    queryKey: ["pools", poolId],
    queryFn: () => fetchPool(poolId),
    enabled: !!poolId,
  })
}

export function usePoolNodes(poolId: string) {
  return useQuery({
    queryKey: ["pools", poolId, "nodes"],
    queryFn: () => fetchPoolNodes(poolId),
    enabled: !!poolId,
  })
}
