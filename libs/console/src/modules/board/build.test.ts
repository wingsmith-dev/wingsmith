import { describe, expect, it } from "vitest"
import {
  assignmentsOf,
  buildBoard,
  fabricHardware,
  liveAssignments,
} from "./build"
import * as make from "@/test/board"

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
  it("draws an allocation as one run even when its cards are not adjacent", () => {
    const cluster = make.cluster("iad-1", "us-east-1")
    const node = make.node("iad-1-n01", cluster.id)
    const fabric = make.domain("tpd_a", cluster.id, node.id)
    const devices = [0, 1, 2, 3, 4, 5, 6, 7].map((index) =>
      make.device(`g${index}`, {
        cluster: cluster.id,
        node: node.id,
        index,
        domain: fabric,
      })
    )
    const held = [devices[0], devices[1], devices[6], devices[7]]
    const board = buildBoard({
      clusters: [cluster],
      nodes: [node],
      domains: [fabric],
      devices,
      assignments: liveAssignments([
        make.allocation("alc_1", {
          cluster: cluster.id,
          workload: "wkl_1",
          devices: held,
        }),
      ]),
    })

    const row = board.regions[0].clusters[0].fabrics[0]
    expect(row.devices.map((cell) => !!cell.holder)).toEqual([
      true,
      true,
      true,
      true,
      false,
      false,
      false,
      false,
    ])
    expect(row.devices.map((cell) => cell.device.id)).toEqual([
      "g0",
      "g1",
      "g6",
      "g7",
      "g2",
      "g3",
      "g4",
      "g5",
    ])
    expect(row.free).toEqual(["g2", "g3", "g4", "g5"])
  })

  it("keeps a fabric over two nodes node by node rather than interleaved", () => {
    const cluster = make.cluster("iad-1", "us-east-1")
    const nodeA = make.node("iad-1-n01", cluster.id)
    const nodeB = make.node("iad-1-n02", cluster.id)
    const fabric = make.domain("tpd_a", cluster.id, nodeA.id)
    const devices = [nodeA, nodeB].flatMap((node) =>
      [0, 1].map((index) =>
        make.device(`${node.name}-g${index}`, {
          cluster: cluster.id,
          node: node.id,
          index,
          domain: fabric,
        })
      )
    )
    const board = buildBoard({
      clusters: [cluster],
      nodes: [nodeA, nodeB],
      domains: [fabric],
      devices: [devices[2], devices[0], devices[3], devices[1]],
      assignments: new Map(),
    })

    const row = board.regions[0].clusters[0].fabrics[0]
    expect(row.devices.map((cell) => cell.device.id)).toEqual([
      "iad-1-n01-g0",
      "iad-1-n01-g1",
      "iad-1-n02-g0",
      "iad-1-n02-g1",
    ])
  })

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

  it("sinks a cluster reporting nothing to the bottom of its region", () => {
    const parts = fleet()
    /**
     * Named so it sorts first alphabetically, which is the whole point: a
     * cluster with no hardware in it must not sit above one that has some.
     */
    const idle = make.cluster("aaa-1", "us-east-1")
    const board = buildBoard({
      ...parts,
      clusters: [...parts.clusters, idle],
      assignments: new Map(),
    })
    const east = board.regions.find((region) => region.region === "us-east-1")!

    expect(east.clusters.map((entry) => entry.cluster.name)).toEqual([
      "iad-1",
      "aaa-1",
    ])
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

  it("nets a cluster's reservations off what it can offer", () => {
    const parts = fleet()
    const board = buildBoard({
      ...parts,
      assignments: new Map(),
      reservations: [
        make.reservation("iad-1", 3, { workloadName: "llama-70b-prod" }),
      ],
    })
    const iad = board.regions
      .flatMap((region) => region.clusters)
      .find((entry) => entry.cluster.name === "iad-1")!

    expect(iad.counts.free).toBe(4)
    expect(iad.counts.reserved).toBe(3)
    expect(iad.counts.available).toBe(1)
    expect(iad.reservations.map((entry) => entry.workload_name)).toEqual([
      "llama-70b-prod",
    ])
  })

  it("paints the promise onto free cells and counts them as free, not held", () => {
    const parts = fleet()
    const board = buildBoard({
      ...parts,
      assignments: new Map(),
      reservations: [make.reservation("iad-1", 3)],
    })
    const painted = [...board.devices.values()].filter(
      (cell) => cell.holder?.origin === "reserved"
    )
    const fabric = board.fabrics.get(board.fabric_of.get("a0")!)!

    expect(painted.map((cell) => cell.device.id)).toEqual(["a0", "a1", "a2"])
    expect(board.counts.held).toBe(0)
    expect(fabric.counts.free).toBe(4)
    expect(fabric.counts.reserved).toBe(3)
    expect(fabric.counts.available).toBe(1)
  })

  it("nets the largest block, so the number agrees with the picture", () => {
    const parts = fleet()
    const board = buildBoard({
      ...parts,
      assignments: new Map(),
      reservations: [make.reservation("iad-1", 3)],
    })
    const iad = board.regions
      .flatMap((region) => region.clusters)
      .find((entry) => entry.cluster.name === "iad-1")!

    expect(iad.counts.available).toBe(1)
    expect(iad.largest_block).toBe(1)
  })

  it("never paints over a device something already holds", () => {
    const parts = fleet()
    const assignments = liveAssignments([
      make.allocation("alc_1", {
        cluster: "iad-1",
        workload: "wkl_a",
        devices: parts.devices.slice(0, 2),
      }),
    ])
    const board = buildBoard({
      ...parts,
      assignments,
      reservations: [make.reservation("iad-1", 4)],
    })

    expect(board.devices.get("a0")!.holder!.origin).toBe("live")
    expect(board.devices.get("a1")!.holder!.origin).toBe("live")
    expect(board.devices.get("a2")!.holder!.origin).toBe("reserved")
    expect(board.devices.get("a3")!.holder!.origin).toBe("reserved")
    expect(board.counts.held).toBe(2)
  })

  it("runs out rather than wrapping when a cluster owes more than it holds", () => {
    const parts = fleet()
    const board = buildBoard({
      ...parts,
      assignments: new Map(),
      reservations: [make.reservation("iad-1", 9)],
    })
    const iad = board.regions
      .flatMap((region) => region.clusters)
      .find((entry) => entry.cluster.name === "iad-1")!
    const painted = [...board.devices.values()].filter(
      (cell) => cell.holder?.origin === "reserved"
    )

    expect(painted).toHaveLength(4)
    expect(iad.counts.reserved).toBe(9)
    expect(iad.counts.available).toBe(0)
  })

  it("paints nothing for a class the cluster does not have free", () => {
    const parts = fleet()
    const board = buildBoard({
      ...parts,
      assignments: new Map(),
      reservations: [make.reservation("iad-1", 2, { class: "l40s-48gb-pcie" })],
    })

    expect(
      [...board.devices.values()].some(
        (cell) => cell.holder?.origin === "reserved"
      )
    ).toBe(false)
  })

  it("gives two reservations different cells, in a stable order", () => {
    const parts = fleet()
    const rows = [
      make.reservation("iad-1", 1, { deployment: "dep_small" }),
      make.reservation("iad-1", 2, { deployment: "dep_big" }),
    ]
    const paint = (reservations: typeof rows) =>
      [
        ...buildBoard({
          ...parts,
          assignments: new Map(),
          reservations,
        }).devices.values(),
      ]
        .filter((cell) => cell.holder?.origin === "reserved")
        .map((cell) => `${cell.device.id}:${cell.holder!.allocation}`)

    expect(paint(rows)).toEqual([
      "a0:reserved:dep_big",
      "a1:reserved:dep_big",
      "a2:reserved:dep_small",
    ])
    expect(paint([...rows].reverse())).toEqual(paint(rows))
  })

  it("never lets one cluster's spare capacity pay another's promise", () => {
    const parts = fleet()
    const board = buildBoard({
      ...parts,
      assignments: new Map(),
      reservations: [make.reservation("iad-1", 10)],
    })

    expect(board.counts.free).toBe(6)
    expect(board.counts.reserved).toBe(10)
    expect(board.counts.available).toBe(2)
  })

  it("reads as it did before when nothing is owed", () => {
    const parts = fleet()
    const board = buildBoard({ ...parts, assignments: new Map() })

    expect(board.counts.reserved).toBe(0)
    expect(board.counts.available).toBe(board.counts.free)
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

describe("fabricHardware", () => {
  it("shouts the model out of the class slug", () => {
    expect(fabricHardware(["h200-141gb-sxm"])).toBe("H200")
  })

  it("names every class of a mixed fabric", () => {
    expect(fabricHardware(["b300-288gb-sxm", "h200-141gb-sxm"])).toBe(
      "B300 + H200"
    )
  })

  it("counts them instead once there are too many to read", () => {
    expect(fabricHardware(["a", "b", "c", "d"])).toBe("Mixed · 4 classes")
  })

  it("says so when there is nothing to name", () => {
    expect(fabricHardware([])).toBe("No devices")
  })
})
