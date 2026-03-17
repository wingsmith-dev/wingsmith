import { useQuery } from "@tanstack/react-query"
import {
  fetchCluster,
  fetchClusterDevices,
  fetchClusterNodes,
  fetchClusters,
  fetchClusterTopology,
} from "./api"

export function useClusters(region?: string) {
  return useQuery({
    queryKey: ["clusters", { region }],
    queryFn: () => fetchClusters(region),
  })
}

export function useCluster(clusterId: string) {
  return useQuery({
    queryKey: ["clusters", clusterId],
    queryFn: () => fetchCluster(clusterId),
    enabled: !!clusterId,
  })
}

export function useClusterNodes(clusterId: string) {
  return useQuery({
    queryKey: ["clusters", clusterId, "nodes"],
    queryFn: () => fetchClusterNodes(clusterId),
    enabled: !!clusterId,
  })
}

export function useClusterDevices(clusterId: string) {
  return useQuery({
    queryKey: ["clusters", clusterId, "devices"],
    queryFn: () => fetchClusterDevices(clusterId),
    enabled: !!clusterId,
  })
}

export function useClusterTopology(clusterId: string) {
  return useQuery({
    queryKey: ["clusters", clusterId, "topology"],
    queryFn: () => fetchClusterTopology(clusterId),
    enabled: !!clusterId,
  })
}
