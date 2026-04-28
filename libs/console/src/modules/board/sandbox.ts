import { isFree } from "./build"
import type {
  Assignments,
  Board,
  BoardCluster,
  BoardDevice,
  BoardFabric,
  BoardMetrics,
  Brick,
  Fit,
  Holder,
  Move,
  PlacementPlan,
  ReplicaPlan,
} from "./types"

/**
 * The live assignments with a plan laid over them.
 *
 * Order matters and is the plan's own: a move frees the devices it came from
 * before it takes the ones it is going to, so two moves that swap places do not
 * annihilate each other. Nothing here touches the plane — the result is a map
 * the board is repainted from and thrown away.
 */
export function applyMoves(base: Assignments, moves: Move[]): Assignments {
  const assignments: Assignments = new Map(base)

  for (const move of moves) {
    if (move.kind === "release") {
      for (const device of move.from) assignments.delete(device)
      continue
    }
    if (move.kind === "move") {
      for (const device of move.from) assignments.delete(device)
      const holder: Holder = { ...move.holder, origin: "moved" }
      for (const device of move.devices) assignments.set(device, holder)
      continue
    }
    const holder = proposedHolder(move)
    for (const device of move.devices) assignments.set(device, holder)
  }

  return assignments
}

function proposedHolder(move: Extract<Move, { kind: "place" }>): Holder {
  return {
    allocation: move.id,
    workload: move.brick.workload ?? move.id,
    workload_name: move.brick.label,
    project_slug: move.brick.project_slug,
    device_class: move.brick.device_class,
    device_count: move.brick.device_count,
    phase: "proposed",
    gang_complete: move.devices.length >= move.brick.device_count,
    origin: "proposed",
  }
}

/** The devices a plan releases, so a re-drop of the same allocation can reuse them. */
export function heldBy(board: Board, allocation: string): string[] {
  const held: string[] = []
  for (const [id, cell] of board.devices) {
    if (cell.holder?.allocation === allocation) held.push(id)
  }
  return held
}

/**
 * Whether a brick fits in one fabric, and which devices it would take.
 *
 * Every refusal names what was wrong rather than returning a bare false: the
 * point of the board is to make "why can't this go here" answerable by pointing
 * at it, and "no" on its own teaches nobody the constraint they just hit.
 */
export function fitOnFabric(
  fabric: BoardFabric,
  brick: Brick,
  assignments: Assignments
): Fit {
  const kind = brick.topology_domain_kind
  if (kind && brick.requires_single_topology_domain && fabric.kind !== kind) {
    return no(`needs ${kind}, this fabric is ${fabric.kind}`)
  }

  const candidates = available(fabric, brick, assignments)
  if (candidates.length === 0) {
    const anyClass = fabric.devices.some(
      (cell) => cell.device.device_class === brick.device_class
    )
    return no(anyClass ? "nothing free here" : `no ${brick.device_class} here`)
  }
  if (candidates.length < brick.device_count) {
    return no(
      `${brick.device_count} needed, ${candidates.length} free in this fabric`
    )
  }
  return {
    ok: true,
    devices: candidates.slice(0, brick.device_count),
    reason: null,
  }
}

/**
 * Whether a brick fits in a cluster, spreading over fabrics when it may.
 *
 * The target fabric is filled first and the rest of the cluster is reached for
 * in descending free order, which is `pack`: a brick that has to span fabrics
 * still leaves the largest blocks whole for something that cannot.
 */
export function fitInCluster(
  cluster: BoardCluster,
  brick: Brick,
  assignments: Assignments,
  preferred?: string
): Fit {
  if (brick.requires_single_topology_domain) {
    const target =
      cluster.fabrics.find((fabric) => fabric.key === preferred) ??
      bestFabric(cluster, brick, assignments)
    if (!target) return no(`no ${brick.device_class} in this cluster`)
    return fitOnFabric(target, brick, assignments)
  }

  const ordered = [...cluster.fabrics].sort((a, b) => {
    if (a.key === preferred) return -1
    if (b.key === preferred) return 1
    return b.free.length - a.free.length
  })

  const taken: string[] = []
  for (const fabric of ordered) {
    for (const device of available(fabric, brick, assignments)) {
      if (taken.length === brick.device_count) break
      taken.push(device)
    }
  }
  if (taken.length < brick.device_count) {
    return no(
      `${brick.device_count} needed, ${taken.length} free in ${cluster.cluster.name}`
    )
  }
  return { ok: true, devices: taken, reason: null }
}

/**
 * A brick dropped on one device: that device, then the next free ones beside it.
 *
 * Starting where the pointer landed rather than at the fabric's first free
 * device is the whole difference between placing something and watching the
 * board decide for you.
 */
export function fitFromDevice(
  board: Board,
  deviceId: string,
  brick: Brick,
  assignments: Assignments
): Fit {
  const fabric = board.fabrics.get(board.fabric_of.get(deviceId) ?? "")
  if (!fabric) return no("device is not on the board")

  const cell = board.devices.get(deviceId)
  if (!cell) return no("device is not on the board")
  if (cell.device.device_class !== brick.device_class) {
    return no(
      `this is a ${cell.device.device_class}, brick is ${brick.device_class}`
    )
  }
  if (!isFree(cell) || assignments.has(deviceId)) return no("already held")

  const fit = fitOnFabric(fabric, brick, assignments)
  if (!fit.ok) return fit

  const rest = available(fabric, brick, assignments).filter(
    (id) => id !== deviceId
  )
  return {
    ok: true,
    devices: [deviceId, ...rest].slice(0, brick.device_count),
    reason: null,
  }
}

/**
 * A brick against the thing the pointer is over.
 *
 * One drop target does for both requirements: a brick that must stay in one
 * fabric is answered by that fabric alone, and one that may spread starts there
 * and reaches into the rest of the cluster only when it has to. Dropping is
 * therefore always meaningful, and the refusal is always about the requirement
 * rather than about where the pointer happened to be.
 */
export function fitOnTarget(
  cluster: BoardCluster,
  fabric: BoardFabric,
  brick: Brick,
  assignments: Assignments
): Fit {
  const here = fitOnFabric(fabric, brick, assignments)
  if (here.ok || brick.requires_single_topology_domain) return here
  return fitInCluster(cluster, brick, assignments, fabric.key)
}

/**
 * A live allocation as a brick, so relocating one is the same operation as
 * placing one.
 *
 * Its topology requirement is not on the allocation — the plane keeps that on
 * the workload and the recipe — so it is read off where the allocation actually
 * sits: everything in one fabric is treated as a gang that has to stay in one.
 * That is a guess, and it is the conservative one.
 */
export function brickOfHolder(board: Board, holder: Holder): Brick {
  const devices = heldBy(board, holder.allocation)
  const fabrics = new Set(
    devices.map((id) => board.fabric_of.get(id)).filter(Boolean)
  )
  return {
    key: holder.allocation,
    label: holder.workload_name,
    detail: `${devices.length}× ${holder.device_class}`,
    workload: holder.workload,
    recipe: null,
    project: null,
    priority_class: null,
    project_slug: holder.project_slug,
    device_class: holder.device_class,
    device_count: devices.length || holder.device_count,
    requires_single_topology_domain: fabrics.size === 1,
    topology_domain_kind: null,
    replicas: 1,
    strategy: "pack",
    cluster_floors: {},
    max_clusters: 1,
    pinned: false,
  }
}

function available(
  fabric: BoardFabric,
  brick: Brick,
  assignments: Assignments
): string[] {
  const open: string[] = []
  const walk = (cell: BoardDevice) => {
    if (cell.slices.length > 0) {
      cell.slices.forEach(walk)
      return
    }
    if (cell.device.device_class !== brick.device_class) return
    if (!isFree(cell)) return
    if (assignments.has(cell.device.id)) return
    open.push(cell.device.id)
  }
  fabric.devices.forEach(walk)
  return open
}

function bestFabric(
  cluster: BoardCluster,
  brick: Brick,
  assignments: Assignments
): BoardFabric | undefined {
  const kind = brick.topology_domain_kind
  return [...cluster.fabrics]
    .filter((fabric) => !kind || fabric.kind === kind)
    .sort(
      (a, b) =>
        available(b, brick, assignments).length -
        available(a, brick, assignments).length
    )[0]
}

function no(reason: string): Fit {
  return { ok: false, devices: [], reason }
}

/**
 * How many replicas of a workload the board already holds.
 *
 * Counted as distinct allocations rather than devices, because that is what a
 * replica is: one allocation, however many devices it took. It is what makes
 * "plan the rest of it" possible — the sandbox places the shortfall against
 * desired scale, not the whole workload over again.
 */
export function replicasPlaced(board: Board, workload: string | null): number {
  if (!workload) return 0
  const seen = new Set<string>()
  for (const cell of board.devices.values()) {
    if (cell.holder?.workload === workload) seen.add(cell.holder.allocation)
  }
  return seen.size
}

interface Floor {
  cluster: string
  remaining: number
}

/**
 * A whole workload placed, replica by replica.
 *
 * This is the part the fit checks above cannot answer on their own. A workload
 * is not a shape, it is n of a shape plus the rules about how those n may be
 * spread: floors are satisfied first because they are the only hard per-cluster
 * demand, `max_clusters` closes the candidate set once it is reached, and
 * `pack` and `spread` differ only in how the remaining replicas are ordered
 * across what is left.
 *
 * Replicas are placed one at a time against a working copy of the assignments,
 * so replica four cannot be handed devices replica two already took. What it
 * does not know is everything placement weighs beyond capacity — quota,
 * priority, preemption, policy, residency — so a plan that fits here is a
 * statement about the fleet's shape and not a prediction of admission.
 */
export function planPlacement(
  board: Board,
  brick: Brick,
  assignments: Assignments,
  wanted: number
): PlacementPlan {
  const clusters = board.regions.flatMap((region) => region.clusters)
  const working: Assignments = new Map(assignments)
  const perCluster = new Map<string, number>()
  const order: string[] = []
  const replicas: ReplicaPlan[] = []
  const unmet: string[] = []
  const floors = floorsOf(brick, clusters)

  for (let index = 1; index <= wanted; index++) {
    const attempt = placeReplica(
      board,
      brick,
      working,
      clusters,
      perCluster,
      floors,
      unmet
    )
    replicas.push({ index, ...attempt })
    if (!attempt.ok || !attempt.cluster) continue

    perCluster.set(attempt.cluster, (perCluster.get(attempt.cluster) ?? 0) + 1)
    if (!order.includes(attempt.cluster)) order.push(attempt.cluster)
    reserve(working, brick, attempt.devices)
    const floor = floors.find((entry) => entry.cluster === attempt.cluster)
    if (floor && floor.remaining > 0) floor.remaining -= 1
  }

  for (const floor of floors) {
    if (floor.remaining <= 0) continue
    const named = clusters.find((entry) => entry.cluster.id === floor.cluster)
    unmet.push(
      `floor unmet · ${named?.cluster.name ?? floor.cluster} short by ${floor.remaining}`
    )
  }

  return {
    brick,
    wanted,
    replicas,
    placed: replicas.filter((replica) => replica.ok).length,
    clusters: order,
    unmet: [...new Set(unmet)],
  }
}

/**
 * The plane's decision, turned into devices.
 *
 * Cluster selection is global and device selection is local — rule 3 — and this
 * is that seam. The plane has already said which clusters and how many replicas
 * each, weighing quota, priority, policy and preemption, none of which the board
 * can see; all that is left is choosing which devices inside those clusters the
 * bricks would sit on, which is the one part the plane deliberately does not
 * decide.
 *
 * A share the board cannot satisfy is reported rather than moved elsewhere.
 * Relocating it would be the console overruling the scheduler, and the honest
 * reading of that disagreement is that the board is looking at a staler fleet
 * than the plane is.
 */
export function planFromShares(
  board: Board,
  brick: Brick,
  assignments: Assignments,
  shares: { cluster: string; replicas: number }[]
): PlacementPlan {
  const clusters = board.regions.flatMap((region) => region.clusters)
  const working: Assignments = new Map(assignments)
  const replicas: ReplicaPlan[] = []
  const unmet: string[] = []
  const order: string[] = []
  let index = 0

  for (const share of shares) {
    const cluster = clusters.find((entry) => entry.cluster.id === share.cluster)
    if (!cluster) {
      for (let i = 0; i < share.replicas; i++) {
        index += 1
        replicas.push({
          index,
          ok: false,
          cluster: share.cluster,
          cluster_name: null,
          fabric: null,
          fabric_identifier: null,
          devices: [],
          reason: "the plane placed this outside the loaded scope",
        })
      }
      continue
    }

    order.push(cluster.cluster.id)
    for (let i = 0; i < share.replicas; i++) {
      index += 1
      const fit = brick.requires_single_topology_domain
        ? tightestFabric(cluster, brick, working)
        : fitInCluster(cluster, brick, working)

      if (!fit.ok) {
        replicas.push({
          index,
          ok: false,
          cluster: cluster.cluster.id,
          cluster_name: cluster.cluster.name,
          fabric: null,
          fabric_identifier: null,
          devices: [],
          reason: `${cluster.cluster.name}: ${fit.reason}`,
        })
        unmet.push(
          `the plane placed a replica in ${cluster.cluster.name}; the board has no room for it`
        )
        continue
      }

      const key = board.fabric_of.get(fit.devices[0]) ?? null
      replicas.push({
        index,
        ok: true,
        cluster: cluster.cluster.id,
        cluster_name: cluster.cluster.name,
        fabric: key,
        fabric_identifier: key ? (board.fabrics.get(key)?.label ?? null) : null,
        devices: fit.devices,
        reason: null,
      })
      reserve(working, brick, fit.devices)
    }
  }

  return {
    brick,
    wanted: shares.reduce((total, share) => total + share.replicas, 0),
    replicas,
    placed: replicas.filter((replica) => replica.ok).length,
    clusters: [...new Set(order)],
    unmet: [...new Set(unmet)],
  }
}

function placeReplica(
  board: Board,
  brick: Brick,
  working: Assignments,
  clusters: BoardCluster[],
  perCluster: Map<string, number>,
  floors: Floor[],
  unmet: string[]
): Omit<ReplicaPlan, "index"> {
  /**
   * Two reasons, because they are not equally worth reading. A cluster that
   * holds this hardware and has none free is the one somebody can act on; a
   * cluster that never had any is noise, and the capacity ordering happily names
   * it first, since an empty cluster ties with every other empty one.
   */
  let relevant: string | null = null
  let fallback: string | null = null

  for (const cluster of candidates(
    brick,
    clusters,
    perCluster,
    floors,
    working
  )) {
    const fit = brick.requires_single_topology_domain
      ? tightestFabric(cluster, brick, working)
      : fitInCluster(cluster, brick, working)

    if (fit.ok) {
      const key = board.fabric_of.get(fit.devices[0]) ?? null
      const fabric = key ? board.fabrics.get(key) : undefined
      return {
        ok: true,
        cluster: cluster.cluster.id,
        cluster_name: cluster.cluster.name,
        fabric: key,
        fabric_identifier: fabric?.label ?? null,
        devices: fit.devices,
        reason: null,
      }
    }

    const line = `${cluster.cluster.name}: ${fit.reason}`
    if (cluster.fabrics.some((fabric) => holdsClass(fabric, brick))) {
      relevant ??= line
    } else {
      fallback ??= line
    }
    const floor = floors.find((entry) => entry.cluster === cluster.cluster.id)
    if (floor && floor.remaining > 0) {
      /**
       * A pin is where the replica goes or nowhere else. A floor is only a
       * minimum, so an unsatisfiable one gives up its claim and lets the replica
       * land elsewhere — recorded, because a shortfall against a declared floor
       * is something somebody has to decide about.
       */
      if (brick.pinned) {
        return {
          ok: false,
          cluster: null,
          cluster_name: null,
          fabric: null,
          fabric_identifier: null,
          devices: [],
          reason: `pinned to ${cluster.cluster.name} · ${fit.reason}`,
        }
      }
      floor.remaining = 0
      unmet.push(line)
      return placeReplica(
        board,
        brick,
        working,
        clusters,
        perCluster,
        floors,
        unmet
      )
    }
  }

  return {
    ok: false,
    cluster: null,
    cluster_name: null,
    fabric: null,
    fabric_identifier: null,
    devices: [],
    reason:
      relevant ??
      fallback ??
      `nothing in the fleet holds ${brick.device_count}× ${brick.device_class}`,
  }
}

/**
 * The clusters to try for one replica, in the order to try them.
 *
 * A pending floor collapses the list to the one cluster that owes replicas;
 * otherwise `max_clusters` closes it to the clusters already used once the
 * ceiling is reached. `pack` then prefers a cluster already carrying replicas of
 * this plan, and `spread` prefers the one carrying fewest.
 */
function candidates(
  brick: Brick,
  clusters: BoardCluster[],
  perCluster: Map<string, number>,
  floors: Floor[],
  assignments: Assignments
): BoardCluster[] {
  const pending = floors.find((entry) => entry.remaining > 0)
  if (pending) {
    const owed = clusters.find((entry) => entry.cluster.id === pending.cluster)
    if (owed) return [owed]
  }

  const ceiling = brick.max_clusters
  const pool =
    ceiling && perCluster.size >= ceiling
      ? clusters.filter((entry) => perCluster.has(entry.cluster.id))
      : clusters

  const free = (cluster: BoardCluster) => freeFor(cluster, brick, assignments)

  return [...pool].sort((a, b) => {
    const held = perCluster.get(a.cluster.id) ?? 0
    const other = perCluster.get(b.cluster.id) ?? 0
    if (brick.strategy === "spread") {
      if (held !== other) return held - other
    } else if (held > 0 !== other > 0) {
      return held > 0 ? -1 : 1
    }
    return free(b) - free(a)
  })
}

/**
 * The tightest fabric a gang still fits in.
 *
 * Best fit rather than first fit, deliberately: dropping an eight-device gang
 * into a sixteen-free fabric when an eight-free one would have taken it destroys
 * the only place the next sixteen-device job could have gone.
 */
function tightestFabric(
  cluster: BoardCluster,
  brick: Brick,
  assignments: Assignments
): Fit {
  const ranked = [...cluster.fabrics]
    .map((fabric) => ({
      fabric,
      open: available(fabric, brick, assignments).length,
    }))
    .filter((entry) => entry.open > 0)
    .sort((a, b) => a.open - b.open)

  let first: Fit | null = null
  for (const entry of ranked) {
    const fit = fitOnFabric(entry.fabric, brick, assignments)
    if (fit.ok) return fit
    first ??= fit
  }
  if (first) return first

  /**
   * A cluster with nothing free and a cluster with none of this hardware are
   * different answers, and only one of them is fixed by waiting.
   */
  const holds = cluster.fabrics.some((fabric) => holdsClass(fabric, brick))
  return no(
    holds
      ? `${brick.device_count} needed, none free in ${cluster.cluster.name}`
      : `no ${brick.device_class} in ${cluster.cluster.name}`
  )
}

/** Whether a fabric carries this class at all, held or not. */
function holdsClass(fabric: BoardFabric, brick: Brick): boolean {
  const walk = (cell: BoardDevice): boolean =>
    cell.slices.length > 0
      ? cell.slices.some(walk)
      : cell.device.device_class === brick.device_class
  return fabric.devices.some(walk)
}

function freeFor(
  cluster: BoardCluster,
  brick: Brick,
  assignments: Assignments
): number {
  return cluster.fabrics.reduce(
    (total, fabric) => total + available(fabric, brick, assignments).length,
    0
  )
}

/** `cluster_floors` resolved against the board, by uid or by name. */
function floorsOf(brick: Brick, clusters: BoardCluster[]): Floor[] {
  const floors: Floor[] = []
  for (const [ref, count] of Object.entries(brick.cluster_floors)) {
    const found = clusters.find(
      (entry) => entry.cluster.id === ref || entry.cluster.name === ref
    )
    if (found && count > 0) {
      floors.push({ cluster: found.cluster.id, remaining: count })
    }
  }
  return floors
}

/** Holds devices for the rest of a planning pass, so no two replicas claim one. */
function reserve(working: Assignments, brick: Brick, devices: string[]): void {
  const holder: Holder = {
    allocation: `pending:${brick.key}`,
    workload: brick.workload ?? brick.key,
    workload_name: brick.label,
    project_slug: brick.project_slug,
    device_class: brick.device_class,
    device_count: brick.device_count,
    phase: "proposed",
    gang_complete: true,
    origin: "proposed",
  }
  for (const device of devices) working.set(device, holder)
}

/** The fleet's shape in numbers, for reading one plan against the live board. */
export function metrics(board: Board): BoardMetrics {
  let largest = 0
  let whole = 0
  let split = 0

  for (const fabric of board.fabrics.values()) {
    const free = fabric.devices.filter(isFree).length
    largest = Math.max(largest, free)
    if (fabric.counts.held === 0 && free > 0) whole += 1
    else if (fabric.counts.held > 0 && free > 0) split += 1
  }

  return {
    total: board.counts.total,
    held: board.counts.held,
    free: board.counts.free,
    largest_block: largest,
    whole_fabrics_free: whole,
    fabrics_split: split,
  }
}

let sequence = 0

/** Plan-local ids. They never leave the browser, so a counter is enough. */
export function moveId(prefix: string): string {
  sequence += 1
  return `${prefix}-${sequence}`
}
