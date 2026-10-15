import { useAllPages, useCursorList } from "@/lib/paginate"
import { fetchClusters } from "@/modules/clusters/api"
import {
  fetchDeployments,
  fetchEndpoints,
  fetchWorkloads,
} from "@/modules/demand/api"
import { fetchDiscoveredWorkloads } from "@/modules/discovered/api"
import { fetchEvents, fetchOperations } from "@/modules/machinery/api"
import { OPEN_PHASES } from "@/modules/machinery/types"
import { fetchDevices, fetchNodes, fetchPools } from "@/modules/supply/api"

/** How many rows the activity panels show. Recent, not a log. */
export const ACTIVITY_LIMIT = 8

/**
 * The drain limit for the fleet aggregates. The overview counts whole
 * collections — "48 of 48 devices healthy" is a lie if it was drawn from a
 * first page — so every tile here reads a drained query, capped at a page size
 * that keeps a three-cluster fleet to a single round trip.
 */
const DRAIN_LIMIT = 200

export function useOverviewClusters() {
  return useAllPages(["overview", "clusters"], (cursor) =>
    fetchClusters({}, cursor, DRAIN_LIMIT)
  )
}

export function useOverviewNodes() {
  return useAllPages(["overview", "nodes"], (cursor) =>
    fetchNodes({}, cursor, DRAIN_LIMIT)
  )
}

export function useOverviewDevices() {
  return useAllPages(["overview", "devices"], (cursor) =>
    fetchDevices({}, cursor, DRAIN_LIMIT)
  )
}

export function useOverviewDeployments() {
  return useAllPages(["overview", "deployments"], (cursor) =>
    fetchDeployments({}, cursor, DRAIN_LIMIT)
  )
}

export function useOverviewEndpoints() {
  return useAllPages(["overview", "endpoints"], (cursor) =>
    fetchEndpoints({}, cursor, DRAIN_LIMIT)
  )
}

export function useOverviewWorkloads() {
  return useAllPages(["overview", "workloads"], (cursor) =>
    fetchWorkloads({}, cursor, DRAIN_LIMIT)
  )
}

/**
 * Sightings nobody has decided about yet. Drained like the rest: an inbox that
 * showed a first page would under-count the very thing it exists to surface.
 */
export function useUndecidedDiscovered() {
  return useAllPages(["overview", "discovered", "new"], (cursor) =>
    fetchDiscoveredWorkloads({ status: "new" }, cursor, DRAIN_LIMIT)
  )
}

/**
 * The newest operations whatever their phase — "what changed", which is a
 * different question from {@link useOpenOperations}'s "what is still in flight".
 */
export function useRecentOperations() {
  return useCursorList(["overview", "operations", "recent"], (cursor) =>
    fetchOperations({}, cursor, ACTIVITY_LIMIT)
  )
}

/**
 * Operations still in flight. Queried per open phase rather than filtered
 * client-side off a page of all operations: a fleet with a long operation
 * history would push the running ones off the first page entirely.
 */
export function useOpenOperations() {
  return useAllPages(["overview", "operations", "open"], async (cursor) => {
    if (cursor) return { items: [], count: 0, next_cursor: null }
    const pages = await Promise.all(
      OPEN_PHASES.map((phase) =>
        fetchOperations({ phase }, undefined, DRAIN_LIMIT)
      )
    )
    const items = pages.flatMap((page) => page.items)
    items.sort((a, b) => b.created_at.localeCompare(a.created_at))
    return { items, count: items.length, next_cursor: null }
  })
}

/**
 * The same drained reads as the fleet tiles, scoped to one cluster. A cluster
 * overview counts its own collections, so every hook here follows the cursor
 * rather than rendering a first page as a total — the one exception is the
 * event feed, which is a recent slice and is labelled as one.
 */
export function useClusterPools(clusterId: string) {
  return useAllPages(["overview", "cluster", clusterId, "pools"], (cursor) =>
    fetchPools({ cluster: clusterId }, cursor, DRAIN_LIMIT)
  )
}

export function useClusterNodes(clusterId: string) {
  return useAllPages(["overview", "cluster", clusterId, "nodes"], (cursor) =>
    fetchNodes({ cluster: clusterId }, cursor, DRAIN_LIMIT)
  )
}

export function useClusterDevices(clusterId: string) {
  return useAllPages(["overview", "cluster", clusterId, "devices"], (cursor) =>
    fetchDevices({ cluster: clusterId }, cursor, DRAIN_LIMIT)
  )
}

export function useClusterDeployments(clusterId: string) {
  return useAllPages(
    ["overview", "cluster", clusterId, "deployments"],
    (cursor) => fetchDeployments({ cluster: clusterId }, cursor, DRAIN_LIMIT)
  )
}

/** Open operations against this cluster, queried per phase for the same reason. */
export function useClusterOpenOperations(clusterId: string) {
  return useAllPages(
    ["overview", "cluster", clusterId, "operations", "open"],
    async (cursor) => {
      if (cursor) return { items: [], count: 0, next_cursor: null }
      const pages = await Promise.all(
        OPEN_PHASES.map((phase) =>
          fetchOperations({ cluster: clusterId, phase }, undefined, DRAIN_LIMIT)
        )
      )
      const items = pages.flatMap((page) => page.items)
      items.sort((a, b) => b.created_at.localeCompare(a.created_at))
      return { items, count: items.length, next_cursor: null }
    }
  )
}

export function useClusterEvents(clusterId: string) {
  return useCursorList(["overview", "cluster", clusterId, "events"], (cursor) =>
    fetchEvents({ cluster: clusterId }, cursor, ACTIVITY_LIMIT)
  )
}
