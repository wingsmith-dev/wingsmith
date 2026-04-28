import type { Cluster } from "@/modules/clusters/types"
import type { Allocation } from "@/modules/machinery/types"
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
  const { clusters, devices, domains, nodes, assignments } = input

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
    return {
      cluster,
      fabrics: group,
      counts: sum(group.map((entry) => entry.counts)),
      largest_block: Math.max(0, ...group.map((entry) => entry.free.length)),
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
  group.sort((a, b) => a.device.device_index - b.device.device_index)
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
    devices: group,
    counts: count(group),
    free: group.filter(isFree).map((cell) => cell.device.id),
  }
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
 * Free means nothing holds it and the cluster says it can be scheduled onto.
 * A partitioned device is never free itself — its slices are counted instead,
 * so an eight-way-split GPU does not read as one spare device.
 */
export function isFree(cell: BoardDevice): boolean {
  if (cell.slices.length > 0) return false
  return cell.holder === null && cell.device.allocatable && !cell.cordoned
}

function count(group: BoardDevice[]): BoardCounts {
  const counts: BoardCounts = {
    total: 0,
    held: 0,
    free: 0,
    fault: 0,
    cordoned: 0,
  }
  const walk = (cell: BoardDevice) => {
    if (cell.slices.length > 0) {
      cell.slices.forEach(walk)
      return
    }
    counts.total += 1
    if (cell.device.health !== "healthy") counts.fault += 1
    else if (cell.cordoned) counts.cordoned += 1
    else if (cell.holder) counts.held += 1
    else if (cell.device.allocatable) counts.free += 1
  }
  group.forEach(walk)
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
    }),
    { total: 0, held: 0, free: 0, fault: 0, cordoned: 0 }
  )
}
