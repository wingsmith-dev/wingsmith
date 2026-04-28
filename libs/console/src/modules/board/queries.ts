import { useAllPages } from "@/lib/paginate"
import { fetchProjects } from "@/modules/binding/api"
import { fetchClusters } from "@/modules/clusters/api"
import { fetchRecipes, fetchWorkloads } from "@/modules/demand/api"
import type { Recipe, Workload } from "@/modules/demand/types"
import { fetchAllocations } from "@/modules/machinery/api"
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
const POLL_MS = 10_000

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
 * Every collection here is read in full rather than a page at a time, because
 * every number on the screen is an aggregate: "how many free devices in one
 * fabric" answered over the first page is not a smaller answer, it is a wrong
 * one. That is affordable because all of it is state — membership, health,
 * allocation — and none of it is telemetry.
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
    }
    return (overlay: Assignments) =>
      buildBoard({ ...parts, assignments: overlay })
  }, [clusters.data, devices.data, domains.data, nodes.data])

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

  return {
    board,
    planned,
    assignments,
    workloads: workloads.data,
    isLoading: parts.some((part) => part.isLoading),
    error: parts.find((part) => part.error)?.error ?? null,
  }
}

/**
 * The bricks, in two kinds, because demand comes in two kinds.
 *
 * A workload brick is a real commitment: it has a scale, spread rules and
 * replicas already holding devices, so planning it means planning the shortfall.
 * A recipe brick is only a shape — what it would take to run this thing — and
 * exists because "where would llama-70b fit" is a question worth asking before
 * there is a workload to ask on behalf of. A fleet with no workloads at all is
 * exactly when the board is most useful, and sourcing bricks from workloads
 * alone left it empty.
 */
export function useBricks() {
  const workloads = useAllPages(["workloads", "bricks"], (cursor) =>
    fetchWorkloads({}, cursor, PAGE)
  )
  const recipes = useAllPages(["recipes", "bricks"], (cursor) =>
    fetchRecipes({}, cursor, PAGE)
  )
  const projects = useAllPages(["projects", "bricks"], (cursor) =>
    fetchProjects({}, cursor, PAGE)
  )

  const bricks = useMemo(() => {
    const byId = new Map(
      (recipes.data ?? []).map((recipe) => [recipe.id, recipe])
    )
    const slugs = new Map(
      (projects.data ?? []).map((project) => [project.id, project.slug])
    )
    return (workloads.data ?? [])
      .map((workload) =>
        brickOf(
          workload,
          byId.get(workload.recipe_id),
          slugs.get(workload.project_id) ?? workload.project_id
        )
      )
      .filter((brick): brick is Brick => brick !== null)
  }, [workloads.data, recipes.data, projects.data])

  /** Published recipes only: a draft is not something anybody is about to run. */
  const shapes = useMemo(
    () =>
      (recipes.data ?? [])
        .filter((recipe) => recipe.lifecycle !== "deprecated")
        .map(brickOfRecipe),
    [recipes.data]
  )

  return {
    bricks,
    shapes,
    isLoading: workloads.isLoading || recipes.isLoading,
  }
}

/**
 * A recipe as a one-replica brick.
 *
 * It carries no strategy, no floors and no ceiling, because a recipe declares
 * none of those — they are the workload's. What it does declare is the whole
 * schedulable requirement: how many devices of what class, and whether they have
 * to share a fabric.
 */
function brickOfRecipe(recipe: Recipe): Brick {
  const shape = recipe.shape
  return {
    key: `rec:${recipe.id}`,
    label: recipe.name,
    detail: [
      `${shape.device_count}× ${shape.device_class}`,
      shape.requires_single_topology_domain
        ? `one ${shape.topology_domain_kind ?? "fabric"}`
        : "may spread",
      recipe.lifecycle === "published" ? null : recipe.lifecycle,
    ]
      .filter(Boolean)
      .join(" · "),
    workload: null,
    recipe: recipe.id,
    /**
     * Filled in by the tray, not here: quota and policy are per project, so the
     * plane cannot be asked about a recipe until somebody says who for.
     */
    project: null,
    priority_class: null,
    project_slug: "unassigned",
    device_class: shape.device_class,
    device_count: shape.device_count,
    requires_single_topology_domain: shape.requires_single_topology_domain,
    topology_domain_kind: shape.topology_domain_kind,
    replicas: 1,
    strategy: "pack",
    cluster_floors: {},
    max_clusters: null,
    pinned: false,
  }
}

function brickOf(
  workload: Workload,
  recipe: Recipe | undefined,
  project: string
): Brick | null {
  if (!recipe) return null
  const shape = recipe.shape
  const single =
    workload.requires_single_topology_domain ??
    shape.requires_single_topology_domain
  const kind = workload.topology_domain_kind ?? shape.topology_domain_kind

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
    recipe: recipe.id,
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
