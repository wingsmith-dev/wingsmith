import { describe, expect, it } from "vitest"
import { assignmentsOf, buildBoard, liveAssignments } from "./build"
import * as make from "./fixture"

function fleet() {
  const iad = make.cluster("iad-1", "us-east-1")
  const fra = make.cluster("fra-1", "eu-central-1")
  const nodeA = make.node("iad-1-n01", iad.id)
  const nodeB = make.node("iad-1-n02", iad.id, { cordoned: true })
  const nodeC = make.node("fra-1-n01", fra.id)
  const fabricA = make.domain("tpd_a", iad.id, nodeA.id)
  const fabricB = make.domain("tpd_b", iad.id, nodeB.id)
  const fabricC = make.domain("tpd_c", fra.id, nodeC.id, "pcie")

  const devices = [
    ...[0, 1, 2, 3].map((index) =>
      make.device(`a${index}`, {
        cluster: iad.id,
        node: nodeA.id,
        index,
        domain: fabricA,
      })
    ),
    make.device("a4", {
      cluster: iad.id,
      node: nodeA.id,
      index: 4,
      domain: fabricA,
      health: "unhealthy",
      allocatable: false,
    }),
    ...[0, 1].map((index) =>
      make.device(`b${index}`, {
        cluster: iad.id,
        node: nodeB.id,
        index,
        domain: fabricB,
      })
    ),
    ...[0, 1].map((index) =>
      make.device(`c${index}`, {
        cluster: fra.id,
        node: nodeC.id,
        index,
        domain: fabricC,
        class: "l40s-48gb-pcie",
      })
    ),
  ]

  return {
    clusters: [iad, fra],
    nodes: [nodeA, nodeB, nodeC],
    domains: [fabricA, fabricB, fabricC],
    devices,
  }
}

describe("liveAssignments", () => {
  it("ignores released allocations and released members", () => {
    const parts = fleet()
    const held = parts.devices.slice(0, 2)
    const gone = parts.devices.slice(2, 4)

    const assignments = liveAssignments([
      make.allocation("alc_live", {
        cluster: "iad-1",
        workload: "wkl_a",
        devices: held,
        releasedDevices: [held[1].id],
      }),
      make.allocation("alc_done", {
        cluster: "iad-1",
        workload: "wkl_b",
        devices: gone,
        live: false,
      }),
    ])

    expect([...assignments.keys()]).toEqual([held[0].id])
    expect(assignments.get(held[0].id)?.origin).toBe("live")
  })
})

describe("buildBoard", () => {
  it("nests region, cluster and fabric and sorts them", () => {
    const parts = fleet()
    const board = buildBoard({ ...parts, assignments: new Map() })

    expect(board.regions.map((region) => region.region)).toEqual([
      "eu-central-1",
      "us-east-1",
    ])
    const east = board.regions[1]
    expect(east.clusters).toHaveLength(1)
    expect(east.clusters[0].fabrics.map((fabric) => fabric.identifier)).toEqual(
      ["iad-1-n01/nvswitch-0", "iad-1-n02/nvswitch-0"]
    )
  })

  it("counts a cordoned node's devices as cordoned, not free", () => {
    const parts = fleet()
    const board = buildBoard({ ...parts, assignments: new Map() })
    const east = board.regions.find((region) => region.region === "us-east-1")!

    expect(east.counts.total).toBe(7)
    expect(east.counts.free).toBe(4)
    expect(east.counts.fault).toBe(1)
    expect(east.counts.cordoned).toBe(2)
    expect(east.clusters[0].largest_block).toBe(4)
  })

  it("hangs MIG slices off the card they were cut from", () => {
    const parts = fleet()
    const parent = parts.devices[0]
    parent.mig_enabled = true
    parent.allocatable = false
    const slices = [0, 1].map((index) =>
      make.device(`a0-mig${index}`, {
        cluster: parent.cluster,
        node: parent.node,
        index,
        domain: parts.domains[0],
        migParent: parent.device_uuid,
        migProfile: "3g.40gb",
        migEnabled: true,
      })
    )

    const board = buildBoard({
      ...parts,
      devices: [...parts.devices, ...slices],
      assignments: new Map(),
    })

    const cell = board.devices.get(parent.id)!
    expect(cell.slices.map((slice) => slice.device.id)).toEqual([
      "a0-mig0",
      "a0-mig1",
    ])
    expect(board.fabric_of.get("a0-mig0")).toBe(board.fabric_of.get(parent.id))

    const fabric = board.fabrics.get(board.fabric_of.get(parent.id)!)!
    expect(fabric.free).not.toContain(parent.id)
    expect(fabric.counts.total).toBe(6)
    expect(fabric.counts.free).toBe(5)
  })

  it("labels a fabric by its nodes when the cluster only reported a uid", () => {
    const parts = fleet()
    const opaque = parts.domains[0]
    for (const device of parts.devices) {
      if (device.topology?.id === opaque.id) {
        device.topology = { ...device.topology, identifier: opaque.id }
      }
    }

    const board = buildBoard({ ...parts, assignments: new Map() })
    const fabric = board.fabrics.get(board.fabric_of.get("a0")!)!

    expect(fabric.identifier).toBe(opaque.id)
    expect(fabric.label).toBe("iad-1-n01")
    expect(fabric.node_names).toEqual(["iad-1-n01"])
  })

  it("keeps an identifier that says something", () => {
    const parts = fleet()
    const board = buildBoard({ ...parts, assignments: new Map() })
    const fabric = board.fabrics.get(board.fabric_of.get("a0")!)!

    expect(fabric.label).toBe("iad-1-n01/nvswitch-0")
  })

  it("reads back the map it was painted from", () => {
    const parts = fleet()
    const assignments = liveAssignments([
      make.allocation("alc_1", {
        cluster: "iad-1",
        workload: "wkl_a",
        devices: parts.devices.slice(0, 3),
      }),
    ])
    const board = buildBoard({ ...parts, assignments })

    expect(assignmentsOf(board)).toEqual(assignments)
    expect(board.counts.held).toBe(3)
  })

  it("drops devices whose cluster was not loaded", () => {
    const parts = fleet()
    const board = buildBoard({
      ...parts,
      clusters: parts.clusters.slice(0, 1),
      assignments: new Map(),
    })

    expect(board.regions.map((region) => region.region)).toEqual(["us-east-1"])
  })
})
