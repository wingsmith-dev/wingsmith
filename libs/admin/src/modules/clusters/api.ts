import { api, type ListResponse } from "@/lib/api"
import type { Device, SupplyNode, TopologyDomain } from "@/modules/supply/types"
import type { Cluster, ClusterDetail } from "./types"

export function fetchClusters(region?: string) {
  return api.get<ListResponse<Cluster>>("/clusters", { region })
}

export async function fetchCluster(clusterId: string) {
  const data = await api.get<{ cluster: ClusterDetail }>(`/clusters/${clusterId}`)
  return data.cluster
}

export function fetchClusterNodes(clusterId: string) {
  return api.get<ListResponse<SupplyNode>>(`/clusters/${clusterId}/nodes`)
}

export function fetchClusterDevices(clusterId: string) {
  return api.get<ListResponse<Device>>(`/clusters/${clusterId}/devices`)
}

export function fetchClusterTopology(clusterId: string) {
  return api.get<ListResponse<TopologyDomain>>(
    `/clusters/${clusterId}/topology-domains`
  )
}
