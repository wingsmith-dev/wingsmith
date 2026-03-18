import { useQuery } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import { fetchCluster, fetchClusters, type ClusterFilters } from "./api"

export function useClusters(filters: ClusterFilters = {}) {
  return useCursorList(["clusters", filters], (cursor) =>
    fetchClusters(filters, cursor)
  )
}

/** Every cluster, drained across pages — for filter controls and name lookups. */
export function useClusterOptions() {
  return useAllPages(["clusters", "options"], (cursor) => fetchClusters({}, cursor, 200))
}

export function useCluster(ref: string) {
  return useQuery({
    queryKey: ["clusters", "detail", ref],
    queryFn: () => fetchCluster(ref),
    enabled: !!ref,
  })
}
