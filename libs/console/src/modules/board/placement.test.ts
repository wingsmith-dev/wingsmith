import { describe, expect, it } from "vitest"
import { buildBoard, liveAssignments } from "./build"
import * as make from "./fixture"
import { planFromShares, planPlacement, replicasPlaced } from "./sandbox"
import type { Assignments, Board } from "./types"

/**
 * A three-cluster fleet across three regions, every cluster holding whole
 * eight-device fabrics of the same class. Uniform on purpose: the rules under
 * test are about how replicas are spread across clusters, so anything that made
 * one cluster the obvious answer would hide which rule did the choosing.
 */
function fleet(options: { fabrics?: Record<string, number> } = {}) {
  const shape = options.fabrics ?? { "iad-1": 3, "sjc-1": 2, "ams-1": 1 }
  const regions: Record<string, string> = {
    "iad-1": "us-east-1",
    "sjc-1": "us-west-2",
    "ams-1": "eu-west-1",
  }

  const clusters = []
  const nodes = []
  const domains = []
  const devices = []

  for (const [name, count] of Object.entries(shape)) {
    const cluster = make.cluster(name, regions[name] ?? "us-east-1")
    clusters.push(cluster)
    for (let index = 0; index < count; index++) {
      const node = make.node(`${name}-n0${index + 1}`, cluster.id)
      const domain = make.domain(`tpd_${name}_${index}`, cluster.id, node.id)
      nodes.push(node)
      domains.push(domain)
      for (let slot = 0; slot < 8; slot++) {
        devices.push(
          make.device(`${name}-${index}-${slot}`, {
            cluster: cluster.id,
            node: node.id,
            index: slot,
            domain,
          })
        )
      }
    }
  }

  const board = buildBoard({
    clusters,
    nodes,
    domains,
    devices,
    assignments: new Map(),
  })
  return { board, assignments: new Map() as Assignments, clusters }
}

/** The clusters a plan used, by name, in the order it reached for them. */
function used(board: Board, ids: string[]): string[] {
  return ids.map(
    (id) =>
      board.regions
        .flatMap((region) => region.clusters)
        .find((entry) => entry.cluster.id === id)?.cluster.name ?? id
  )
}

describe("planFromShares", () => {
  it("paints the plane's shares onto devices in the clusters it named", () => {
    const { board, clusters, assignments } = fleet()
    const iad = clusters.find((entry) => entry.name === "iad-1")!
    const ams = clusters.find((entry) => entry.name === "ams-1")!

    const plan = planFromShares(
      board,
      make.brick({ device_count: 8 }),
      assignments,
      [
        { cluster: iad.id, replicas: 2 },
        { cluster: ams.id, replicas: 1 },
      ]
    )

    expect(plan.wanted).toBe(3)
    expect(plan.placed).toBe(3)
    expect(plan.replicas.map((replica) => replica.cluster_name)).toEqual([
      "iad-1",
      "iad-1",
      "ams-1",
    ])
    const all = plan.replicas.flatMap((replica) => replica.devices)
    expect(new Set(all).size).toBe(24)
  })

  it("reports a share the board cannot seat rather than moving it elsewhere", () => {
    const { board, clusters, assignments } = fleet({
      fabrics: { "iad-1": 3, "ams-1": 1 },
    })
    const ams = clusters.find((entry) => entry.name === "ams-1")!

    const plan = planFromShares(
      board,
      make.brick({ device_count: 8 }),
      assignments,
      [{ cluster: ams.id, replicas: 2 }]
    )

    expect(plan.placed).toBe(1)
    expect(plan.replicas[1].ok).toBe(false)
    expect(plan.replicas[1].cluster_name).toBe("ams-1")
    expect(plan.unmet.join(" ")).toContain("ams-1")
    expect(
      plan.replicas.some((replica) => replica.cluster_name === "iad-1")
    ).toBe(false)
  })

  it("says so when the plane placed outside the loaded scope", () => {
    const { board, assignments } = fleet()
    const plan = planFromShares(board, make.brick(), assignments, [
      { cluster: "clu_not_loaded", replicas: 1 },
    ])

    expect(plan.placed).toBe(0)
    expect(plan.replicas[0].reason).toContain("outside the loaded scope")
  })
})

describe("planPlacement", () => {
  it("places every replica when the fleet has room", () => {
    const { board, assignments } = fleet()
    const plan = planPlacement(
      board,
      make.brick({ device_count: 8, replicas: 4 }),
      assignments,
      4
    )

    expect(plan.placed).toBe(4)
    expect(plan.replicas.every((replica) => replica.ok)).toBe(true)
    expect(new Set(plan.replicas.flatMap((r) => r.devices)).size).toBe(32)
  })

  it("never hands two replicas the same device", () => {
    const { board, assignments } = fleet()
    const plan = planPlacement(
      board,
      make.brick({ device_count: 4, replicas: 6 }),
      assignments,
      6
    )
    const all = plan.replicas.flatMap((replica) => replica.devices)

    expect(plan.placed).toBe(6)
    expect(new Set(all).size).toBe(all.length)
  })

  it("packs into one cluster before reaching for the next", () => {
    const { board, assignments } = fleet()
    const plan = planPlacement(
      board,
      make.brick({ device_count: 8, replicas: 3, strategy: "pack" }),
      assignments,
      3
    )

    expect(plan.clusters).toHaveLength(1)
    expect(used(board, plan.clusters)).toEqual(["iad-1"])
  })

  it("spreads across clusters, levelling them", () => {
    const { board, assignments } = fleet()
    const plan = planPlacement(
      board,
      make.brick({ device_count: 8, replicas: 3, strategy: "spread" }),
      assignments,
      3
    )

    expect(new Set(plan.clusters).size).toBe(3)
    expect(plan.replicas.map((replica) => replica.cluster_name).sort()).toEqual(
      ["ams-1", "iad-1", "sjc-1"]
    )
  })

  it("honours max_clusters even when spreading", () => {
    const { board, assignments } = fleet()
    const plan = planPlacement(
      board,
      make.brick({
        device_count: 8,
        replicas: 4,
        strategy: "spread",
        max_clusters: 2,
      }),
      assignments,
      4
    )

    expect(plan.placed).toBe(4)
    expect(new Set(plan.clusters).size).toBe(2)
  })

  it("satisfies a floor before anything else, then carries on", () => {
    const { board, clusters, assignments } = fleet()
    const ams = clusters.find((entry) => entry.name === "ams-1")!
    const plan = planPlacement(
      board,
      make.brick({
        device_count: 8,
        replicas: 3,
        strategy: "pack",
        cluster_floors: { [ams.id]: 1 },
      }),
      assignments,
      3
    )

    expect(plan.replicas[0].cluster_name).toBe("ams-1")
    expect(plan.placed).toBe(3)
    expect(plan.unmet).toEqual([])
  })

  it("reports a floor the fleet cannot honour without losing the rest", () => {
    const { board, clusters, assignments } = fleet()
    const ams = clusters.find((entry) => entry.name === "ams-1")!
    const plan = planPlacement(
      board,
      make.brick({
        device_count: 8,
        replicas: 3,
        cluster_floors: { [ams.id]: 3 },
      }),
      assignments,
      3
    )

    expect(plan.placed).toBe(3)
    expect(plan.unmet.length).toBeGreaterThan(0)
    expect(plan.unmet.join(" ")).toContain("ams-1")
    expect(
      plan.replicas.filter((replica) => replica.cluster_name === "ams-1")
    ).toHaveLength(1)
  })

  it("keeps a pinned workload on its cluster or nowhere", () => {
    const { board, clusters, assignments } = fleet({
      fabrics: { "iad-1": 3, "ams-1": 1 },
    })
    const ams = clusters.find((entry) => entry.name === "ams-1")!
    const plan = planPlacement(
      board,
      make.brick({
        device_count: 8,
        replicas: 3,
        cluster_floors: { [ams.id]: 3 },
        max_clusters: 1,
        pinned: true,
      }),
      assignments,
      3
    )

    expect(plan.placed).toBe(1)
    expect(plan.replicas[0].cluster_name).toBe("ams-1")
    const failed = plan.replicas.filter((replica) => !replica.ok)
    expect(failed).toHaveLength(2)
    expect(failed[0].reason).toContain("pinned to ams-1")
    expect(plan.clusters).toEqual([ams.id])
  })

  it("ignores a floor naming a cluster the board does not hold", () => {
    const { board, assignments } = fleet()
    const plan = planPlacement(
      board,
      make.brick({
        device_count: 8,
        replicas: 2,
        cluster_floors: { clu_gone: 2 },
      }),
      assignments,
      2
    )

    expect(plan.placed).toBe(2)
    expect(plan.unmet).toEqual([])
  })

  it("places what it can and says why the rest could not", () => {
    const { board, assignments } = fleet({ fabrics: { "iad-1": 2 } })
    const plan = planPlacement(
      board,
      make.brick({ device_count: 8, replicas: 4 }),
      assignments,
      4
    )

    expect(plan.placed).toBe(2)
    const failed = plan.replicas.filter((replica) => !replica.ok)
    expect(failed).toHaveLength(2)
    expect(failed[0].reason).toContain("none free")
    expect(failed[0].index).toBe(3)
  })

  it("takes the tightest fabric a gang fits in, leaving big blocks whole", () => {
    const iad = make.cluster("iad-1", "us-east-1")
    const big = make.node("iad-1-big", iad.id)
    const small = make.node("iad-1-small", iad.id)
    const bigFabric = make.domain("tpd_big", iad.id, big.id)
    const smallFabric = make.domain("tpd_small", iad.id, small.id)
    const devices = [
      ...Array.from({ length: 16 }, (_, index) =>
        make.device(`big-${index}`, {
          cluster: iad.id,
          node: big.id,
          index,
          domain: bigFabric,
        })
      ),
      ...Array.from({ length: 8 }, (_, index) =>
        make.device(`small-${index}`, {
          cluster: iad.id,
          node: small.id,
          index,
          domain: smallFabric,
        })
      ),
    ]
    const board = buildBoard({
      clusters: [iad],
      nodes: [big, small],
      domains: [bigFabric, smallFabric],
      devices,
      assignments: new Map(),
    })

    const plan = planPlacement(
      board,
      make.brick({ device_count: 8, replicas: 1 }),
      new Map(),
      1
    )

    expect(plan.replicas[0].fabric_identifier).toBe("iad-1-small/nvswitch-0")
  })

  it("plans around what is already held", () => {
    const iad = make.cluster("iad-1", "us-east-1")
    const nodes = [
      make.node("iad-1-n01", iad.id),
      make.node("iad-1-n02", iad.id),
    ]
    const domains = [
      make.domain("tpd_1", iad.id, nodes[0].id),
      make.domain("tpd_2", iad.id, nodes[1].id),
    ]
    const devices = [
      ...Array.from({ length: 8 }, (_, index) =>
        make.device(`n1-${index}`, {
          cluster: iad.id,
          node: nodes[0].id,
          index,
          domain: domains[0],
        })
      ),
      ...Array.from({ length: 8 }, (_, index) =>
        make.device(`n2-${index}`, {
          cluster: iad.id,
          node: nodes[1].id,
          index,
          domain: domains[1],
        })
      ),
    ]
    const assignments = liveAssignments([
      make.allocation("alc_1", {
        cluster: iad.id,
        workload: "wkl_1",
        devices: devices.slice(0, 8),
      }),
    ])
    const board = buildBoard({
      clusters: [iad],
      nodes,
      domains,
      devices,
      assignments,
    })

    expect(replicasPlaced(board, "wkl_1")).toBe(1)

    const plan = planPlacement(
      board,
      make.brick({ workload: "wkl_1", device_count: 8, replicas: 2 }),
      assignments,
      2 - replicasPlaced(board, "wkl_1")
    )

    expect(plan.wanted).toBe(1)
    expect(plan.placed).toBe(1)
    expect(plan.replicas[0].fabric_identifier).toBe("iad-1-n02/nvswitch-0")
  })
})
