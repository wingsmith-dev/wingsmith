import type { Cluster } from "@/modules/clusters/types"
import type { Allocation } from "@/modules/machinery/types"
import type { Reservation } from "@/modules/placement/types"
import type { Device, SupplyNode, TopologyDomain } from "@/modules/supply/types"
import type {
  Assignments,
  Board,
  BoardCluster,
  BoardCounts,
  BoardDevice,
  BoardFabric,
  BoardRegion,
  Holder,
} from "./types"

const UNPLACED = "no-topology"

/**
 * What holds what, right now, from the observed allocations.
 *
 * Released allocations and released members are skipped: an allocation is how a
 * finished commitment stays auditable, and a device it no longer holds is free
 * regardless of the row still existing.
 */
export function liveAssignments(allocations: Allocation[]): Assignments {
  const assignments: Assignments = new Map()
  for (const allocation of allocations) {
    if (!allocation.live) continue
    const holder = holderOf(allocation)
    for (const member of allocation.devices) {
      if (!member.live) continue
      assignments.set(member.device, holder)
    }
  }
  return assignments
}

export function holderOf(allocation: Allocation): Holder {
  return {
    allocation: allocation.id,
    workload: allocation.workload,
    workload_name: allocation.workload,
    project_slug: allocation.project_slug,
    device_class: allocation.device_class,
    device_count: allocation.device_count,
    phase: allocation.phase,
    gang_complete: allocation.gang_complete,
    origin: "live",
  }
}

/** Resolves the workload uids on holders to the names people read. */
export function nameHolders(
  assignments: Assignments,
  names: Map<string, string>
): Assignments {
  const named: Assignments = new Map()
  for (const [device, holder] of assignments) {
    named.set(device, {
      ...holder,
      workload_name: names.get(holder.workload) ?? holder.workload_name,
    })
  }
  return named
}

export interface BoardInput {
  clusters: Cluster[]
  devices: Device[]
  domains: TopologyDomain[]
  nodes: SupplyNode[]
  assignments: Assignments
  /**
   * What the plane says is already spoken for. Optional, and absent means none
   * are known rather than none exist: the board is built from observed state and
   * must still render when the reservations call has not answered or has failed,
   * because a fleet drawn without its promises is worse than one drawn with none.
   */
  reservations?: Reservation[]
}

/**
 * The five layers, nested: region, cluster, fabric, device, slice.
 *
 * Built from observed state and one assignment map, so the live board and a
 * sandbox board are the same function over different maps.
 *
 * Every cluster handed in gets a place on the board, including the ones holding
 * nothing: a cluster that reports no devices is a fact worth seeing, and an
 * enrolled cluster silently missing from a capacity view is the kind of absence
 * nobody notices. Devices whose cluster was not loaded go the other way and are
 * dropped rather than invented.
 */
export function buildBoard(input: BoardInput): Board {
  const board = assemble(input)
  const overlay = reservedOverlay(board, input.reservations ?? [])
  if (overlay.size === 0) return board
  return assemble({
    ...input,
    assignments: new Map([...input.assignments, ...overlay]),
  })
}

/**
 * One pass over the inputs. Everything `buildBoard` does twice, it does here.
 *
 * Split out because a reservation has to be laid onto cells that are free, and
 * "free" is a fact about the finished board — which fabric a device sits in, in
 * what order, with what already on it. Building once, choosing the cells, and
 * building again is the honest way to say that; the alternative is a second
 * implementation of fabric grouping that would drift from this one.
 */
function assemble(input: BoardInput): Board {
  const { clusters, devices, domains, nodes, assignments } = input
  const owed = byCluster(input.reservations ?? [])

  const cordoned = new Set(
    nodes.filter((node) => node.cordoned).map((node) => node.id)
  )
  const domainById = new Map(domains.map((domain) => [domain.id, domain]))
  const nodeName = new Map(nodes.map((node) => [node.id, node.name]))

  const slicesOf = new Map<string, Device[]>()
  const physical: Device[] = []
  for (const device of devices) {
    const parent = device.mig_parent_device_uuid
    if (parent) {
      const bucket = slicesOf.get(parent)
      if (bucket) bucket.push(device)
      else slicesOf.set(parent, [device])
    } else {
      physical.push(device)
    }
  }

  const cells = new Map<string, BoardDevice>()
  const byFabric = new Map<string, BoardDevice[]>()
  const fabricOf = new Map<string, string>()

  for (const device of physical) {
    const key = fabricKey(device)
    const slices = (slicesOf.get(device.device_uuid) ?? [])
      .sort((a, b) => a.device_index - b.device_index)
      .map((slice) => cell(slice, assignments, cordoned, []))
    const parent = cell(device, assignments, cordoned, slices)

    cells.set(device.id, parent)
    fabricOf.set(device.id, key)
    for (const slice of slices) {
      cells.set(slice.device.id, slice)
      fabricOf.set(slice.device.id, key)
    }

    const bucket = byFabric.get(key)
    if (bucket) bucket.push(parent)
    else byFabric.set(key, [parent])
  }

  const fabrics = new Map<string, BoardFabric>()
  for (const [key, group] of byFabric) {
    fabrics.set(key, fabric(key, group, domainById, nodeName))
  }

  const fabricsByCluster = new Map<string, BoardFabric[]>()
  for (const entry of fabrics.values()) {
    const bucket = fabricsByCluster.get(entry.cluster)
    if (bucket) bucket.push(entry)
    else fabricsByCluster.set(entry.cluster, [entry])
  }

  const boardClusters: BoardCluster[] = clusters.map((cluster) => {
    const group = fabricsByCluster.get(cluster.id) ?? []
    group.sort((a, b) => a.label.localeCompare(b.label))
    const reservations = owed.get(cluster.id) ?? []
    return {
      cluster,
      fabrics: group,
      counts: promised(sum(group.map((entry) => entry.counts)), reservations),
      largest_block: Math.max(0, ...group.map((entry) => entry.free.length)),
      reservations,
    }
  })

  const byRegion = new Map<string, BoardCluster[]>()
  for (const entry of boardClusters) {
    const region = entry.cluster.region
    const bucket = byRegion.get(region)
    if (bucket) bucket.push(entry)
    else byRegion.set(region, [entry])
  }

  const regions: BoardRegion[] = [...byRegion.entries()]
    .map(([region, group]) => ({
      region,
      clusters: group.sort((a, b) =>
        a.cluster.name.localeCompare(b.cluster.name)
      ),
      counts: sum(group.map((entry) => entry.counts)),
    }))
    .sort((a, b) => a.region.localeCompare(b.region))

  return {
    regions,
    counts: sum(regions.map((region) => region.counts)),
    devices: cells,
    fabrics,
    fabric_of: fabricOf,
  }
}

/**
 * The board read back as the map it was painted from. Fit checks work off this
 * rather than off a map threaded alongside the board, so what they answer about
 * is always the board on screen — live or planned — and never a stale copy.
 */
export function assignmentsOf(board: Board): Assignments {
  const assignments: Assignments = new Map()
  for (const [id, cell] of board.devices) {
    if (cell.holder) assignments.set(id, cell.holder)
  }
  return assignments
}

function fabricKey(device: Device): string {
  return device.topology?.id ?? `${device.node}/${UNPLACED}`
}

function cell(
  device: Device,
  assignments: Assignments,
  cordoned: Set<string>,
  slices: BoardDevice[]
): BoardDevice {
  return {
    device,
    holder: assignments.get(device.id) ?? null,
    cordoned: cordoned.has(device.node),
    slices,
  }
}

function fabric(
  key: string,
  group: BoardDevice[],
  domains: Map<string, TopologyDomain>,
  nodeName: Map<string, string>
): BoardFabric {
  group.sort(
    (a, b) =>
      a.device.node_name.localeCompare(b.device.node_name) ||
      a.device.device_index - b.device.device_index
  )
  const laid = blocks(group)
  const first = group[0].device
  const domain = domains.get(first.topology?.id ?? "") ?? null
  const nodes = [...new Set(group.map((cell) => cell.device.node))]
  const names = nodes.map(
    (node) =>
      nodeName.get(node) ??
      group.find((cell) => cell.device.node === node)?.device.node_name ??
      node
  )
  const classes = new Set(group.map((cell) => cell.device.device_class))
  const identifier = first.topology?.identifier ?? `${names[0]} · no fabric`

  return {
    key,
    identifier,
    label: informative(first.topology) ? identifier : cover(names),
    kind: first.topology?.kind ?? UNPLACED,
    domain,
    cluster: first.cluster,
    cluster_name: first.cluster_name,
    nodes,
    node_names: names,
    device_class: classes.size === 1 ? first.device_class : null,
    device_classes: [...classes].sort(),
    devices: laid,
    counts: count(laid),
    free: laid.filter(isFree).map((cell) => cell.device.id),
  }
}

/**
 * The hardware a fabric is, in the few characters a person scans a board for.
 *
 * The plane names a class with a slug — `h200-141gb-sxm` — and the memory and
 * the form factor of it are already spoken for elsewhere on the row. What is
 * left is the model, so the first segment is taken and shouted.
 *
 * A fabric of several classes names all of them rather than picking a winner,
 * and past three it stops trying: this label is a glance, not an inventory.
 */
export function fabricHardware(classes: string[]): string {
  if (classes.length === 0) return "No devices"
  if (classes.length > 3) return `Mixed · ${classes.length} classes`
  return classes.map(model).join(" + ")
}

function model(deviceClass: string): string {
  const head = deviceClass.split("-")[0]
  return head ? head.toUpperCase() : deviceClass
}

/**
 * A fabric's cells laid out so that what holds several of them reads as one run.
 *
 * The row is sorted by node and device index first, and this only closes the
 * gaps: cells are emitted in blocks of one holder at a time, each block placed
 * where its lowest-numbered card sits, with the unheld cells of that node
 * collected into a block of their own. Nothing is invented and nothing moves
 * between nodes — a card keeps its node, its index, and the tooltip that names
 * both.
 *
 * It is a deliberate departure from drawing the row as the chassis is numbered,
 * and the reason is what the board is for. A cluster's device plugin is free to
 * hand a four-card gang gpu0, gpu1, gpu6 and gpu7, and drawn literally that is
 * two pairs with a hole between them — a picture that says "no room for four
 * here" about a fabric with room for four. The question this board answers is
 * how much contiguous capacity a fabric has, so its cells are grouped by what
 * holds them. `Inspector` and the tooltip remain the place the physical index is
 * read off.
 */
function blocks(cells: BoardDevice[]): BoardDevice[] {
  const order: string[] = []
  const groups = new Map<string, BoardDevice[]>()
  for (const cell of cells) {
    const key = `${cell.device.node}\u0000${cell.holder?.allocation ?? ""}`
    const bucket = groups.get(key)
    if (bucket) bucket.push(cell)
    else {
      groups.set(key, [cell])
      order.push(key)
    }
  }
  return order.flatMap((key) => groups.get(key) ?? [])
}

/**
 * Whether the cluster's identifier for a domain is worth reading. An identifier
 * that is the domain's own uid is the cluster declining to name it, which is
 * allowed — `identifier` is only unique per cluster and kind, so a cluster with
 * nothing better to say says the uid.
 */
function informative(topology: Device["topology"]): boolean {
  return !!topology && topology.identifier !== topology.id
}

/** The nodes a fabric covers, short enough for a label column. */
function cover(names: string[]): string {
  if (names.length === 0) return "unknown node"
  if (names.length === 1) return names[0]
  return `${names[0]} +${names.length - 1} more`
}

/**
 * Free means nothing holds it, nothing is owed it, and the cluster says it can
 * be scheduled onto. A partitioned device is never free itself — its slices are
 * counted instead, so an eight-way-split GPU does not read as one spare device.
 *
 * A reserved cell is excluded, and that is what stops the sandbox from planning
 * onto capacity the fleet has already promised somebody. It is still counted as
 * `free` in the tally, because it genuinely is unheld — the two answers differ
 * because the questions do: "what is nobody using" and "what could I take".
 */
export function isFree(cell: BoardDevice): boolean {
  if (cell.slices.length > 0) return false
  if (cell.holder) return false
  return cell.device.allocatable && !cell.cordoned
}

/** Whether a holder is an allocation the cluster actually reported. */
export function isBound(holder: Holder | null): boolean {
  return !!holder && holder.origin !== "reserved"
}

/**
 * Which cells to paint for each reservation, and it is a guess by construction.
 *
 * The plane knows a cluster owes six h100s and does not know which six — device
 * selection is the cluster's, not ours (rule 3). Leaving the board blank was the
 * first answer and it was the wrong one: a cluster reading "0 available" with
 * eight visibly idle squares and nothing to point at is exactly the confusion
 * this feature exists to remove. So the board picks plausible cells, and every
 * label attached to one says the cluster has not chosen yet.
 *
 * Deterministic, so the picture does not shuffle under somebody every ten
 * seconds: reservations are taken in a stable order, and each takes free cells
 * of its own class in board order. A reservation that must land in one fabric
 * gets the tightest fabric that can still hold it whole — the same best-fit rule
 * the sandbox uses, and the likeliest thing the cluster will actually do —
 * falling back to spreading when no single fabric can. A cluster owing more than
 * it has free simply runs out: what there is gets painted and the rest does not,
 * because `counts.reserved` already carries the true number and the two are
 * different questions.
 *
 * Nothing here ever displaces a live holder. It only ever consumes what the
 * fabric already reported as free.
 */
export function reservedOverlay(
  board: Board,
  reservations: Reservation[]
): Assignments {
  const overlay: Assignments = new Map()
  if (reservations.length === 0) return overlay

  const owed = byCluster(reservations)
  for (const cluster of board.regions.flatMap((region) => region.clusters)) {
    const rows = [...(owed.get(cluster.cluster.id) ?? [])].sort(order)
    if (rows.length === 0) continue

    const taken = new Set<string>()
    for (const row of rows) {
      const holder = reservedHolder(row)
      for (const id of cellsFor(board, cluster, row, taken)) {
        taken.add(id)
        overlay.set(id, holder)
      }
    }
  }
  return overlay
}

/**
 * A stable order for the reservations of one cluster, so the painting is the
 * same on every poll. By size first, so the promise hardest to satisfy gets the
 * whole fabric while there is one, then by name and uid to break ties.
 */
function order(a: Reservation, b: Reservation): number {
  if (a.devices !== b.devices) return b.devices - a.devices
  const named = (row: Reservation) =>
    row.deployment_name ?? row.workload_name ?? ""
  return (
    named(a).localeCompare(named(b)) ||
    (a.deployment ?? a.workload).localeCompare(b.deployment ?? b.workload)
  )
}

function reservedHolder(row: Reservation): Holder {
  return {
    allocation: `reserved:${row.deployment ?? row.workload}`,
    workload: row.workload,
    workload_name: row.workload_name ?? row.workload,
    project_slug: row.project,
    device_class: row.device_class,
    device_count: row.devices,
    phase: "reserved",
    gang_complete: false,
    origin: "reserved",
  }
}

/** The free cells of one fabric this reservation could use, in device order. */
function openIn(
  board: Board,
  fabric: BoardFabric,
  row: Reservation,
  taken: Set<string>
): string[] {
  return fabric.free.filter(
    (id) =>
      !taken.has(id) &&
      board.devices.get(id)?.device.device_class === row.device_class
  )
}

/**
 * The fabric kind is a preference and never a filter, which matters at the very
 * end. A gang that names `nvswitch` should land on an nvswitch fabric wherever
 * one can take it — but a cluster owing eight of them with nothing suitable free
 * must still show the promise somewhere, or the reserved numeral goes back to
 * having nothing on the board to point at, which is the whole bug. Better a
 * roughly-placed square with an honest label than an invisible one.
 */
function cellsFor(
  board: Board,
  cluster: BoardCluster,
  row: Reservation,
  taken: Set<string>
): string[] {
  const kind = row.requires_single_domain ? row.domain_kind : null
  const preferred = kind
    ? cluster.fabrics.filter((fabric) => fabric.kind === kind)
    : cluster.fabrics
  const ordered = [
    ...preferred,
    ...cluster.fabrics.filter((fabric) => !preferred.includes(fabric)),
  ]

  if (row.requires_single_domain) {
    const whole = (group: BoardFabric[]) =>
      group
        .map((fabric) => openIn(board, fabric, row, taken))
        .filter((open) => open.length >= row.devices)
        .sort((a, b) => a.length - b.length)[0]
    const fits = whole(preferred) ?? whole(ordered)
    if (fits) return fits.slice(0, row.devices)
  }

  const spread: string[] = []
  for (const fabric of ordered) {
    for (const id of openIn(board, fabric, row, taken)) {
      if (spread.length === row.devices) return spread
      spread.push(id)
    }
  }
  return spread
}

/** Reservations bucketed by the cluster that owes them, in the order given. */
function byCluster(reservations: Reservation[]): Map<string, Reservation[]> {
  const owed = new Map<string, Reservation[]>()
  for (const reservation of reservations) {
    const bucket = owed.get(reservation.cluster)
    if (bucket) bucket.push(reservation)
    else owed.set(reservation.cluster, [reservation])
  }
  return owed
}

/**
 * One cluster's tally with the exact number it owes put back in.
 *
 * The fabrics below it counted the cells the board painted, which is at most
 * what the cluster had free; this replaces that with what the plane actually
 * says is owed. Applied at the cluster and nowhere below, because the cluster is
 * the layer the plane made the statement about. A region and the fleet get their
 * numbers by summing these, which keeps the clamp where it belongs — an
 * over-subscribed cluster contributes zero available capacity and never a
 * negative that quietly cancels out somebody else's spare.
 */
function promised(
  counts: BoardCounts,
  reservations: Reservation[]
): BoardCounts {
  const reserved = reservations.reduce(
    (total, reservation) => total + reservation.devices,
    0
  )
  return {
    ...counts,
    reserved,
    available: Math.max(0, counts.free - reserved),
  }
}

function count(group: BoardDevice[]): BoardCounts {
  const counts: BoardCounts = {
    total: 0,
    held: 0,
    free: 0,
    fault: 0,
    cordoned: 0,
    reserved: 0,
    available: 0,
  }
  const walk = (cell: BoardDevice) => {
    if (cell.slices.length > 0) {
      cell.slices.forEach(walk)
      return
    }
    counts.total += 1
    if (cell.device.health !== "healthy") counts.fault += 1
    else if (cell.cordoned) counts.cordoned += 1
    else if (isBound(cell.holder)) counts.held += 1
    else if (cell.device.allocatable) {
      counts.free += 1
      if (cell.holder) counts.reserved += 1
    }
  }
  group.forEach(walk)
  counts.available = Math.max(0, counts.free - counts.reserved)
  return counts
}

function sum(all: BoardCounts[]): BoardCounts {
  return all.reduce(
    (acc, counts) => ({
      total: acc.total + counts.total,
      held: acc.held + counts.held,
      free: acc.free + counts.free,
      fault: acc.fault + counts.fault,
      cordoned: acc.cordoned + counts.cordoned,
      reserved: acc.reserved + counts.reserved,
      available: acc.available + counts.available,
    }),
    {
      total: 0,
      held: 0,
      free: 0,
      fault: 0,
      cordoned: 0,
      reserved: 0,
      available: 0,
    }
  )
}
