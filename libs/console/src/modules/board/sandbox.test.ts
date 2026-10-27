import { describe, expect, it } from "vitest"
import { assignmentsOf, buildBoard, liveAssignments } from "./build"
import * as make from "@/test/board"
import {
  applyMoves,
  brickOfHolder,
  fitFromDevice,
  fitInCluster,
  fitOnFabric,
  fitOnTarget,
  heldBy,
  metrics,
} from "./sandbox"
import type { Assignments, Board } from "./types"

/**
 * One cluster, two eight-device nvswitch fabrics and one four-device pcie
 * fabric. Small enough that every expectation below can be counted by hand.
 */
function scene(options: { held?: number } = {}) {
  const iad = make.cluster("iad-1", "us-east-1")
  const nodes = ["n01", "n02", "n03"].map((suffix) =>
    make.node(`iad-1-${suffix}`, iad.id)
  )
  const fabrics = [
    make.domain("tpd_1", iad.id, nodes[0].id),
    make.domain("tpd_2", iad.id, nodes[1].id),
    make.domain("tpd_3", iad.id, nodes[2].id, "pcie"),
  ]

  const devices = [
    ...Array.from({ length: 8 }, (_, index) =>
      make.device(`n1-${index}`, {
        cluster: iad.id,
        node: nodes[0].id,
        index,
        domain: fabrics[0],
      })
    ),
    ...Array.from({ length: 8 }, (_, index) =>
      make.device(`n2-${index}`, {
        cluster: iad.id,
        node: nodes[1].id,
        index,
        domain: fabrics[1],
      })
    ),
    ...Array.from({ length: 4 }, (_, index) =>
      make.device(`n3-${index}`, {
        cluster: iad.id,
        node: nodes[2].id,
        index,
        domain: fabrics[2],
        class: "l40s-48gb-pcie",
      })
    ),
  ]

  const allocations = options.held
    ? [
        make.allocation("alc_1", {
          cluster: iad.id,
          workload: "wkl_llama",
          devices: devices.slice(0, options.held),
        }),
      ]
    : []

  const assignments = liveAssignments(allocations)
  const board = buildBoard({
    clusters: [iad],
    nodes,
    domains: fabrics,
    devices,
    assignments,
  })
  return { board, assignments, cluster: board.regions[0].clusters[0] }
}

function fabric(board: Board, identifier: string) {
  return [...board.fabrics.values()].find(
    (entry) => entry.identifier === identifier
  )!
}

describe("fitOnFabric", () => {
  it("takes exactly what the brick asks for", () => {
    const { board, assignments } = scene()
    const fit = fitOnFabric(
      fabric(board, "iad-1-n01/nvswitch-0"),
      make.brick({ device_count: 4 }),
      assignments
    )

    expect(fit.ok).toBe(true)
    expect(fit.devices).toEqual(["n1-0", "n1-1", "n1-2", "n1-3"])
  })

  it("names the shortfall, not just refusing", () => {
    const { board, assignments } = scene({ held: 6 })
    const fit = fitOnFabric(
      fabric(board, "iad-1-n01/nvswitch-0"),
      make.brick({ device_count: 4 }),
      assignments
    )

    expect(fit.ok).toBe(false)
    expect(fit.reason).toContain("4 needed, 2 free")
  })

  it("refuses a class the fabric does not hold", () => {
    const { board, assignments } = scene()
    const fit = fitOnFabric(
      fabric(board, "iad-1-n03/pcie-0"),
      make.brick({ device_count: 2 }),
      assignments
    )

    expect(fit.ok).toBe(false)
    expect(fit.reason).toContain("no h100-80gb-sxm here")
  })

  it("refuses a fabric of the wrong kind for a gang that names one", () => {
    const { board, assignments } = scene()
    const fit = fitOnFabric(
      fabric(board, "iad-1-n03/pcie-0"),
      make.brick({
        device_class: "l40s-48gb-pcie",
        device_count: 2,
        topology_domain_kind: "nvswitch",
      }),
      assignments
    )

    expect(fit.ok).toBe(false)
    expect(fit.reason).toContain("needs nvswitch")
  })
})

describe("fitInCluster", () => {
  it("spreads a brick that may spread across fabrics", () => {
    const { board, assignments, cluster } = scene({ held: 6 })
    const fit = fitInCluster(
      cluster,
      make.brick({
        device_count: 6,
        requires_single_topology_domain: false,
      }),
      assignments,
      fabric(board, "iad-1-n01/nvswitch-0").key
    )

    expect(fit.ok).toBe(true)
    expect(fit.devices.slice(0, 2)).toEqual(["n1-6", "n1-7"])
    expect(fit.devices).toHaveLength(6)
  })

  it("keeps a gang in one fabric even when the cluster has the devices", () => {
    const { board, assignments, cluster } = scene({ held: 6 })
    const spread = fitInCluster(
      cluster,
      make.brick({ device_count: 6, requires_single_topology_domain: false }),
      assignments,
      fabric(board, "iad-1-n01/nvswitch-0").key
    )
    const gang = fitOnTarget(
      cluster,
      fabric(board, "iad-1-n01/nvswitch-0"),
      make.brick({ device_count: 6 }),
      assignments
    )

    expect(spread.ok).toBe(true)
    expect(gang.ok).toBe(false)
  })

  it("falls through to the rest of the cluster when the drop fabric is full", () => {
    const { board, assignments, cluster } = scene({ held: 8 })
    const fit = fitOnTarget(
      cluster,
      fabric(board, "iad-1-n01/nvswitch-0"),
      make.brick({ device_count: 3, requires_single_topology_domain: false }),
      assignments
    )

    expect(fit.ok).toBe(true)
    expect(fit.devices).toEqual(["n2-0", "n2-1", "n2-2"])
  })
})

describe("fitFromDevice", () => {
  it("starts where the pointer landed", () => {
    const { board, assignments } = scene()
    const fit = fitFromDevice(
      board,
      "n1-5",
      make.brick({ device_count: 3 }),
      assignments
    )

    expect(fit.ok).toBe(true)
    expect(fit.devices[0]).toBe("n1-5")
    expect(fit.devices).toHaveLength(3)
  })

  it("refuses a device something already holds", () => {
    const { board, assignments } = scene({ held: 2 })
    const fit = fitFromDevice(
      board,
      "n1-0",
      make.brick({ device_count: 1 }),
      assignments
    )

    expect(fit.ok).toBe(false)
    expect(fit.reason).toBe("already held")
  })
})

describe("applyMoves", () => {
  const rebuild = (base: ReturnType<typeof scene>, assignments: Assignments) =>
    buildBoard({
      clusters: [base.cluster.cluster],
      nodes: base.cluster.fabrics.flatMap((entry) =>
        entry.nodes.map((id) => make.node(id, base.cluster.cluster.id))
      ),
      domains: base.cluster.fabrics
        .map((entry) => entry.domain)
        .filter((entry): entry is NonNullable<typeof entry> => entry !== null),
      devices: [...base.board.devices.values()].map((cell) => cell.device),
      assignments,
    })

  it("paints a placed brick as proposed", () => {
    const base = scene()
    const brick = make.brick({ device_count: 2, label: "new-thing" })
    const after = applyMoves(base.assignments, [
      { id: "plan-1", kind: "place", brick, devices: ["n2-0", "n2-1"] },
    ])

    expect(after.get("n2-0")?.origin).toBe("proposed")
    expect(after.get("n2-0")?.workload_name).toBe("new-thing")
    expect(after.get("n2-0")?.gang_complete).toBe(true)
  })

  it("frees the devices a moved allocation came from", () => {
    const base = scene({ held: 4 })
    const holder = base.assignments.get("n1-0")!
    const from = heldBy(base.board, holder.allocation)

    const after = applyMoves(base.assignments, [
      {
        id: "plan-1",
        kind: "move",
        allocation: holder.allocation,
        holder,
        from,
        devices: ["n2-0", "n2-1", "n2-2", "n2-3"],
      },
    ])

    expect(from).toEqual(["n1-0", "n1-1", "n1-2", "n1-3"])
    for (const id of from) expect(after.has(id)).toBe(false)
    expect(after.get("n2-3")?.origin).toBe("moved")
  })

  it("frees everything a released allocation held", () => {
    const base = scene({ held: 4 })
    const holder = base.assignments.get("n1-0")!
    const after = applyMoves(base.assignments, [
      {
        id: "plan-1",
        kind: "release",
        allocation: holder.allocation,
        holder,
        from: heldBy(base.board, holder.allocation),
      },
    ])

    expect(after.size).toBe(0)
  })

  it("lets two moves swap places without annihilating each other", () => {
    const base = scene({ held: 4 })
    const holder = base.assignments.get("n1-0")!
    const after = applyMoves(base.assignments, [
      {
        id: "plan-1",
        kind: "move",
        allocation: holder.allocation,
        holder,
        from: heldBy(base.board, holder.allocation),
        devices: ["n2-0", "n2-1", "n2-2", "n2-3"],
      },
      {
        id: "plan-2",
        kind: "place",
        brick: make.brick({ device_count: 4, label: "second" }),
        devices: ["n1-0", "n1-1", "n1-2", "n1-3"],
      },
    ])

    expect(after.get("n1-0")?.workload_name).toBe("second")
    expect(after.get("n2-0")?.workload_name).toBe(holder.workload_name)
    expect(after.size).toBe(8)
  })

  it("moves the largest free block when a plan repacks a fabric", () => {
    const base = scene({ held: 4 })
    const before = metrics(base.board)
    const holder = base.assignments.get("n1-0")!

    const planned = rebuild(
      base,
      applyMoves(base.assignments, [
        {
          id: "plan-1",
          kind: "move",
          allocation: holder.allocation,
          holder,
          from: heldBy(base.board, holder.allocation),
          devices: ["n2-0", "n2-1", "n2-2", "n2-3"],
        },
      ])
    )
    const after = metrics(planned)

    expect(before.largest_block).toBe(8)
    expect(before.fabrics_split).toBe(1)
    expect(after.largest_block).toBe(8)
    expect(after.free).toBe(before.free)
    expect(assignmentsOf(planned).size).toBe(4)
  })
})

describe("brickOfHolder", () => {
  it("treats an allocation inside one fabric as a gang that stays there", () => {
    const base = scene({ held: 4 })
    const holder = base.assignments.get("n1-0")!
    const brick = brickOfHolder(base.board, holder)

    expect(brick.device_count).toBe(4)
    expect(brick.requires_single_topology_domain).toBe(true)
  })

  it("lets an allocation already spanning fabrics keep spanning them", () => {
    const iad = make.cluster("iad-1", "us-east-1")
    const nodes = ["n01", "n02"].map((suffix) =>
      make.node(`iad-1-${suffix}`, iad.id)
    )
    const fabrics = [
      make.domain("tpd_1", iad.id, nodes[0].id),
      make.domain("tpd_2", iad.id, nodes[1].id),
    ]
    const devices = [
      make.device("n1-0", {
        cluster: iad.id,
        node: nodes[0].id,
        index: 0,
        domain: fabrics[0],
      }),
      make.device("n2-0", {
        cluster: iad.id,
        node: nodes[1].id,
        index: 0,
        domain: fabrics[1],
      }),
    ]
    const assignments = liveAssignments([
      make.allocation("alc_split", {
        cluster: iad.id,
        workload: "wkl_split",
        devices,
      }),
    ])
    const board = buildBoard({
      clusters: [iad],
      nodes,
      domains: fabrics,
      devices,
      assignments,
    })

    const brick = brickOfHolder(board, assignments.get("n1-0")!)
    expect(brick.requires_single_topology_domain).toBe(false)
  })
})
