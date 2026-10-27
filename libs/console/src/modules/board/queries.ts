import { POLL } from "@/lib/poll"
import { useAllPages } from "@/lib/paginate"
import { fetchProjects } from "@/modules/binding/api"
import { fetchClusters } from "@/modules/clusters/api"
import { fetchWorkloads } from "@/modules/demand/api"
import type { Workload } from "@/modules/demand/types"
import { fetchAllocations } from "@/modules/machinery/api"
import { useReservations } from "@/modules/placement/queries"
import {
  fetchDevices,
  fetchNodes,
  fetchTopologyDomains,
} from "@/modules/supply/api"
import { useMemo } from "react"
import { buildBoard, liveAssignments, nameHolders } from "./build"
import { applyMoves } from "./sandbox"
import type { Assignments, Board, Brick, Move } from "./types"

const PAGE = 200

/** Poll cadence for the board. Allocations move on the sync stream's timescale. */
const POLL_MS = POLL.allocations

export interface BoardScope {
  cluster?: string
  /** Device class, sent to the plane as `class`. */
  class?: string
  /** The plan to lay over the observed allocations. Empty is the live board. */
  moves?: Move[]
}

/**
 * The whole board, drained.
 *
 * Every collection here is read in full instead of a page at a time, because
 * every number on the screen is an aggregate: "how many free devices in one
 * fabric" answered over the first page is not a smaller answer, it is a wrong
 * one. That is affordable because all of it is state (membership, health,
 * allocation) and none of it is telemetry.
 */
export function useFleetBoard(scope: BoardScope = {}) {
  const clusters = useAllPages(["clusters", "board"], (cursor) =>
    fetchClusters({}, cursor, PAGE)
  )
  const nodes = useAllPages(
    ["nodes", "board", scope.cluster ?? null],
    (cursor) => fetchNodes({ cluster: scope.cluster }, cursor, PAGE)
  )
  const devices = useAllPages(
    ["devices", "board", scope.cluster ?? null, scope.class ?? null],
    (cursor) =>
      fetchDevices({ cluster: scope.cluster, class: scope.class }, cursor, PAGE)
  )
  const domains = useAllPages(
    ["topology-domains", "board", scope.cluster ?? null],
    (cursor) => fetchTopologyDomains({ cluster: scope.cluster }, cursor, PAGE)
  )
  const allocations = useAllPages(
    ["allocations", "board", scope.cluster ?? null],
    (cursor) =>
      fetchAllocations({ cluster: scope.cluster, live: true }, cursor, PAGE),
    { refetchInterval: POLL_MS }
  )
  const workloads = useAllPages(["workloads", "board"], (cursor) =>
    fetchWorkloads({}, cursor, PAGE)
  )

  /**
   * The declared half, and the only thing on this page that the plane derives
   * instead of the cluster reporting it.
   *
   * It is scoped exactly the way the devices are, so a board filtered to one
   * cluster or one device class does not carry a reserved total counted over
   * hardware that is not on the screen.
   */
  const reservations = useReservations({
    cluster: scope.cluster,
    device_class: scope.class,
  })

  const assignments: Assignments | undefined = useMemo(() => {
    if (!allocations.data) return undefined
    const names = new Map(
      (workloads.data ?? []).map((workload) => [workload.id, workload.name])
    )
    return nameHolders(liveAssignments(allocations.data), names)
  }, [allocations.data, workloads.data])

  const build = useMemo(() => {
    if (!clusters.data || !devices.data || !domains.data || !nodes.data) {
      return undefined
    }
    const parts = {
      clusters: clusters.data,
      devices: devices.data,
      domains: domains.data,
      nodes: nodes.data,
      reservations: reservations.data ?? [],
    }
    return (overlay: Assignments) =>
      buildBoard({ ...parts, assignments: overlay })
  }, [clusters.data, devices.data, domains.data, nodes.data, reservations.data])

  const board: Board | undefined = useMemo(
    () => (build && assignments ? build(assignments) : undefined),
    [build, assignments]
  )

  /**
   * The same board with the plan applied. It is the live board itself when the
   * plan is empty, so an untouched sandbox and the live view are the same object
   * and cannot drift apart.
   */
  const planned: Board | undefined = useMemo(() => {
    const moves = scope.moves ?? []
    if (!build || !assignments || moves.length === 0) return board
    return build(applyMoves(assignments, moves))
  }, [build, assignments, board, scope.moves])

  const parts = [clusters, nodes, devices, domains, allocations, workloads]

  /**
   * Reservations are reported but never waited for. Everything else here is what
   * the board *is* (no devices means no board), while a missing reserved total
   * costs one row of numerals. Holding the whole page behind it would make the
   * declared half of the answer able to hide the observed half, which is
   * backwards: the observed half is the one an operator can act on.
   */
  return {
    board,
    planned,
    assignments,
    workloads: workloads.data,
    isLoading: parts.some((part) => part.isLoading),
    error:
      parts.find((part) => part.error)?.error ?? reservations.error ?? null,
  }
}

/**
 * The bricks: every workload as the shape it asks the fleet for.
 *
 * A workload brick is a real commitment: it has a scale, spread rules and
 * replicas already holding devices, so planning it means planning the shortfall.
 * A serving configuration is not an object, so "where would llama-70b fit"
 * before anything is declared is asked of an artifact's fitment or of the shape
 * builder, both of which the tray offers.
 */
export function useBricks() {
  const workloads = useAllPages(["workloads", "bricks"], (cursor) =>
    fetchWorkloads({}, cursor, PAGE)
  )
  const projects = useAllPages(["projects", "bricks"], (cursor) =>
    fetchProjects({}, cursor, PAGE)
  )

  const bricks = useMemo(() => {
    const slugs = new Map(
      (projects.data ?? []).map((project) => [project.id, project.slug])
    )
    return (workloads.data ?? []).map((workload) =>
      brickOf(workload, slugs.get(workload.project_id) ?? workload.project_id)
    )
  }, [workloads.data, projects.data])

  return {
    bricks,
    isLoading: workloads.isLoading,
  }
}

function brickOf(workload: Workload, project: string): Brick {
  const shape = workload.serving.shape
  const single = shape.requires_single_topology_domain
  const kind = shape.topology_domain_kind

  const replicas = Math.max(workload.scale.desired, 0)
  const detail = [
    `${replicas} × ${shape.device_count}× ${shape.device_class}`,
    single ? `one ${kind ?? "fabric"}` : "may spread",
    workload.strategy,
    workload.max_clusters ? `max ${workload.max_clusters} clusters` : null,
    workload.pinned_cluster_id ? "pinned" : null,
  ]
    .filter(Boolean)
    .join(" · ")

  return {
    key: workload.id,
    label: workload.name,
    detail,
    workload: workload.id,
    project: workload.project_id,
    priority_class: workload.priority_class,
    project_slug: project,
    device_class: shape.device_class,
    device_count: shape.device_count,
    replicas,
    strategy: workload.strategy,
    /**
     * A pin is carried as a floor of the workload's whole scale on the one
     * cluster, which is what names the cluster for the planner. `pinned` beside
     * it is what stops that floor behaving like a floor: an unmet floor lets its
     * replicas land elsewhere, and an unmet pin does not.
     */
    cluster_floors: workload.pinned_cluster_id
      ? { [workload.pinned_cluster_id]: replicas }
      : workload.cluster_floors,
    max_clusters: workload.pinned_cluster_id ? 1 : workload.max_clusters,
    pinned: workload.pinned_cluster_id !== null,
    requires_single_topology_domain: single,
    topology_domain_kind: kind,
  }
}
