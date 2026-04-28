import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { Cluster } from "@/modules/clusters/types"
import type { Deployment, Endpoint } from "@/modules/demand/types"
import type { Operation } from "@/modules/machinery/types"
import type { Device, Pool, SupplyNode } from "@/modules/supply/types"
import {
  deriveClusterActionables,
  summariseClusters,
  summariseDemand,
  summariseDevices,
  summariseFleet,
  summariseNodes,
} from "./summary"

const NOW = new Date("2026-08-23T12:00:00Z")
const MINUTES = 60 * 1000

// Factories take only what a test asserts on; the rest is filled in as something plausible.
// Casting is deliberate — restating forty wire fields per fixture would hide the one that matters.
function sync(over: Partial<Cluster["sync"]> = {}): Cluster["sync"] {
  return {
    state: "connected",
    last_seen_at: NOW.toISOString(),
    last_sync_at: NOW.toISOString(),
    last_error: null,
    operator_version: "1.4.0",
    lag_ms: 0,
    ...over,
  } as Cluster["sync"]
}

function replicas(
  over: Partial<Deployment["replicas"]> = {}
): Deployment["replicas"] {
  return {
    total: 2,
    ready: 2,
    running: 2,
    pending: 0,
    failed: 0,
    at_current_generation: 2,
    ...over,
  } as Deployment["replicas"]
}

function cluster(over: Partial<Cluster> = {}): Cluster {
  return {
    id: "clu_1",
    name: "iad-1",
    phase: "active",
    reachable: true,
    sync: sync(),
    ...over,
  } as Cluster
}

function node(over: Partial<SupplyNode> = {}): SupplyNode {
  return {
    id: "nod_1",
    name: "iad-1-n01",
    cluster: "clu_1",
    ready: true,
    cordoned: false,
    condition: "ready",
    observed_at: NOW.toISOString(),
    pool_name: "gpu",
    ...over,
  } as SupplyNode
}

function device(over: Partial<Device> = {}): Device {
  return {
    id: "dev_1",
    cluster: "clu_1",
    node_name: "iad-1-n01",
    device_index: 0,
    device_class: "h100-80gb-sxm",
    health: "healthy",
    health_reason: null,
    allocatable: true,
    throttled: false,
    throttle_reasons: [],
    ...over,
  } as Device
}

function pool(over: Partial<Pool> = {}): Pool {
  return {
    id: "pol_1",
    name: "gpu",
    device_class: "h100-80gb-sxm",
    enabled: true,
    min_nodes: 2,
    max_nodes: 8,
    within_declared_bounds: true,
    membership: { nodes_total: 4 },
    ...over,
  } as Pool
}

function deployment(over: Partial<Deployment> = {}): Deployment {
  return {
    id: "dep_1",
    name: "qwen3-8b",
    cluster_id: "clu_1",
    workload_name: "qwen3-8b",
    enabled: true,
    desired_replicas: 2,
    replicas: replicas(),
    admission_phase: "running",
    admission_reason: null,
    placed_at: new Date(NOW.getTime() - 60 * MINUTES).toISOString(),
    admitted_at: null,
    queued_at: null,
    ...over,
  } as Deployment
}

function ids(rows: { id: string }[]): string[] {
  return rows.map((r) => r.id)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})
afterEach(() => {
  vi.useRealTimers()
})

// --- rollups ---------------------------------------------------------------

describe("summariseClusters", () => {
  it("counts reachability and sync apart from phase", () => {
    const got = summariseClusters([
      cluster(),
      cluster({
        id: "clu_2",
        reachable: false,
        sync: sync({ state: "disconnected" }),
      }),
      cluster({
        id: "clu_3",
        sync: sync({ state: "disconnected" }),
      }),
      cluster({ id: "clu_4", phase: "draining" }),
    ])
    expect(got).toEqual({
      total: 4,
      active: 3,
      unreachable: 1,
      outOfSync: 2,
    })
  })

  it("is all zeroes for an empty fleet rather than throwing", () => {
    expect(summariseClusters([])).toEqual({
      total: 0,
      active: 0,
      unreachable: 0,
      outOfSync: 0,
    })
  })
})

describe("summariseNodes", () => {
  it("counts a cordon and a fault separately, because they need different responses", () => {
    // A cordoned node is a drain someone asked for; a not-ready node is a fault.
    const got = summariseNodes([
      node(),
      node({ id: "nod_2", cordoned: true }),
      node({ id: "nod_3", ready: false }),
    ])
    expect(got).toEqual({ total: 3, ready: 2, cordoned: 1, notReady: 1 })
  })

  it("counts a node that is both cordoned and not ready in both", () => {
    const got = summariseNodes([node({ ready: false, cordoned: true })])
    expect(got.cordoned).toBe(1)
    expect(got.notReady).toBe(1)
    expect(got.ready).toBe(0)
  })
})

describe("summariseDevices", () => {
  it("lists impaired devices rather than counting them", () => {
    // One degraded card disappears into "47 of 48 healthy"; the panel exists so it does not.
    const got = summariseDevices([
      ...Array.from({ length: 47 }, (_, i) => device({ id: `dev_${i}` })),
      device({ id: "dev_bad", health: "degraded" }),
    ])
    expect(got.total).toBe(48)
    expect(got.healthy).toBe(47)
    expect(ids(got.impaired)).toEqual(["dev_bad"])
  })

  it("counts a throttled but healthy device as impaired", () => {
    const got = summariseDevices([
      device({ id: "dev_hot", throttled: true, throttle_reasons: ["thermal"] }),
    ])
    expect(ids(got.impaired)).toEqual(["dev_hot"])
    expect(got.healthy).toBe(1)
  })

  it("orders health worst first so the loudest thing is at the top", () => {
    const got = summariseDevices([
      device({ id: "a", health: "unknown" }),
      device({ id: "b", health: "healthy" }),
      device({ id: "c", health: "unhealthy" }),
      device({ id: "d", health: "degraded" }),
    ])
    expect(got.byHealth.map((h) => h.health)).toEqual([
      "healthy",
      "degraded",
      "unhealthy",
      "unknown",
    ])
    // impaired is ordered worst-rank-first, which puts "unknown" ahead of "unhealthy".
    expect(got.impaired.map((d) => d.health)).toEqual([
      "unknown",
      "unhealthy",
      "degraded",
    ])
  })

  it("groups by class, largest first, and counts healthy within each", () => {
    const got = summariseDevices([
      device({ id: "a", device_class: "h100-80gb-sxm" }),
      device({ id: "b", device_class: "h100-80gb-sxm" }),
      device({ id: "c", device_class: "h100-80gb-sxm", health: "degraded" }),
      device({ id: "d", device_class: "a100-80gb-sxm" }),
    ])
    expect(got.byClass).toEqual([
      { deviceClass: "h100-80gb-sxm", count: 3, healthy: 2 },
      { deviceClass: "a100-80gb-sxm", count: 1, healthy: 1 },
    ])
  })

  it("counts allocatable apart from healthy", () => {
    // A healthy card already holding a workload is not spare capacity.
    const got = summariseDevices([
      device({ id: "a", allocatable: true }),
      device({ id: "b", allocatable: false }),
    ])
    expect(got.healthy).toBe(2)
    expect(got.allocatable).toBe(1)
  })

  it("handles an empty fleet", () => {
    const got = summariseDevices([])
    expect(got).toMatchObject({ total: 0, healthy: 0, allocatable: 0 })
    expect(got.byHealth).toEqual([])
    expect(got.impaired).toEqual([])
  })
})

describe("summariseFleet", () => {
  it("counts each cluster only from what belongs to it", () => {
    const got = summariseFleet(
      [cluster(), cluster({ id: "clu_2", name: "sin-1" })],
      [node(), node({ id: "nod_2", cluster: "clu_2", ready: false })],
      [
        device({ id: "dev_1" }),
        device({ id: "dev_2", cluster: "clu_2", health: "unhealthy" }),
      ],
      [deployment(), deployment({ id: "dep_2", cluster_id: "clu_2" })]
    )

    expect(got[0].nodes.total).toBe(1)
    expect(got[0].devices).toEqual({ total: 1, healthy: 1 })
    expect(got[1].nodes.notReady).toBe(1)
    expect(got[1].devices).toEqual({ total: 1, healthy: 0 })
    expect(got[1].deployments).toBe(1)
  })

  it("gives a cluster with nothing on it a row of zeroes rather than omitting it", () => {
    const got = summariseFleet([cluster()], [], [], [])
    expect(got).toHaveLength(1)
    expect(got[0].nodes.total).toBe(0)
    expect(got[0].deployments).toBe(0)
  })

  it("ignores rows belonging to a cluster that is not listed", () => {
    const got = summariseFleet(
      [cluster()],
      [node({ id: "nod_x", cluster: "clu_gone" })],
      [],
      []
    )
    expect(got[0].nodes.total).toBe(0)
  })
})

describe("summariseDemand", () => {
  it("sums replicas across deployments", () => {
    const got = summariseDemand(
      [
        deployment(),
        deployment({
          id: "dep_2",
          desired_replicas: 4,
          replicas: replicas({ ready: 1, pending: 3, failed: 0 }),
          admission_phase: "pending",
        }),
      ],
      [
        { id: "end_1", enabled: true } as Endpoint,
        { id: "end_2", enabled: false } as Endpoint,
      ]
    )
    expect(got).toEqual({
      deployments: 2,
      deploymentsPending: 1,
      replicasReady: 3,
      replicasDesired: 6,
      endpoints: 2,
      endpointsEnabled: 1,
    })
  })
})

// --- what needs attention --------------------------------------------------

describe("deriveClusterActionables", () => {
  it("says nothing about a healthy cluster", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      pools: [pool()],
      nodes: [node()],
      devices: [device()],
      deployments: [deployment()],
      operations: [],
    })
    expect(got).toEqual([])
  })

  it("reports an unreachable cluster once, not three times", () => {
    // An unreachable cluster also has a broken sync link and a stale last sync.
    const got = deriveClusterActionables({
      cluster: cluster({
        reachable: false,
        sync: sync({
          state: "disconnected",
          last_seen_at: new Date(NOW.getTime() - 60 * MINUTES).toISOString(),
          last_sync_at: new Date(NOW.getTime() - 60 * MINUTES).toISOString(),
          last_error: "connection refused",
        }),
      }),
    })
    expect(ids(got)).toEqual(["cluster:unreachable"])
    expect(got[0].tone).toBe("danger")
  })

  it("reports a cluster that never connected as not enrolled, not a failure", () => {
    // Nothing has broken; it has not been enrolled yet. `reachable` is false
    // because the plane has no sync row for it — the shape the previous version
    // of this test could not produce, having left the fixture default of true.
    const got = deriveClusterActionables({
      cluster: cluster({
        reachable: false,
        sync: sync({ state: "never_connected" }),
      }),
    })
    expect(ids(got)).toEqual(["cluster:not-enrolled"])
    expect(got[0].tone).toBe("info")
  })

  it("treats a link that dropped as a failure", () => {
    const got = deriveClusterActionables({
      cluster: cluster({ sync: sync({ state: "disconnected" }) }),
    })
    expect(got[0].tone).toBe("danger")
  })

  it("does not add a stale-sync row on top of a broken link", () => {
    const stale = new Date(NOW.getTime() - 60 * MINUTES).toISOString()
    const got = deriveClusterActionables({
      cluster: cluster({
        sync: sync({ state: "disconnected", last_sync_at: stale }),
      }),
    })
    expect(ids(got)).toEqual(["cluster:sync"])
  })

  it("flags a stale sync on an otherwise connected cluster", () => {
    const got = deriveClusterActionables({
      cluster: cluster({
        sync: sync({
          last_sync_at: new Date(NOW.getTime() - 6 * MINUTES).toISOString(),
        }),
      }),
    })
    expect(ids(got)).toEqual(["cluster:stale"])
    expect(got[0].detail).toMatch(/at least that old/)
  })

  it("does not flag a sync that is merely a few minutes old", () => {
    const got = deriveClusterActionables({
      cluster: cluster({
        sync: sync({
          last_sync_at: new Date(NOW.getTime() - 4 * MINUTES).toISOString(),
        }),
      }),
    })
    expect(got).toEqual([])
  })

  it("states a pool shortfall as the two numbers rather than a ratio", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      pools: [
        pool({ min_nodes: 4, membership: { nodes_total: 3 } } as Partial<Pool>),
      ],
    })
    expect(ids(got)).toEqual(["pool:under:pol_1"])
    expect(got[0].title).toContain("wants 4 nodes, has 3")
    expect(got[0].tone).toBe("danger")
  })

  it("treats being over the maximum as a warning, not a failure", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      pools: [
        pool({ max_nodes: 2, membership: { nodes_total: 4 } } as Partial<Pool>),
      ],
    })
    expect(ids(got)).toEqual(["pool:over:pol_1"])
    expect(got[0].tone).toBe("warn")
  })

  it("reports a pool once, by the most specific rule that applies", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      pools: [
        pool({
          min_nodes: 4,
          membership: { nodes_total: 1 },
          within_declared_bounds: false,
        } as Partial<Pool>),
      ],
    })
    expect(ids(got)).toEqual(["pool:under:pol_1"])
  })

  it("names an unhealthy device louder than a degraded one", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      devices: [
        device({ id: "dev_bad", health: "unhealthy", health_reason: "ECC" }),
        device({ id: "dev_warm", health: "degraded" }),
      ],
    })
    expect(got.map((r) => r.tone)).toEqual(["danger", "warn"])
    expect(got[0].detail).toBe("ECC")
  })

  it("says a device is throttled in the same row rather than a second one", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      devices: [
        device({
          id: "dev_hot",
          health: "degraded",
          throttled: true,
          throttle_reasons: ["thermal"],
        }),
      ],
    })
    expect(got).toHaveLength(1)
    expect(got[0].title).toContain("throttled")
    expect(got[0].badges).toContain("thermal")
  })

  it("demotes a cordon to information, because someone asked for it", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      nodes: [node({ cordoned: true })],
    })
    expect(ids(got)).toEqual(["node:cordoned:nod_1"])
    expect(got[0].tone).toBe("info")
  })

  it("reports a not-ready node as a fault", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      nodes: [node({ ready: false, condition: "disk_pressure" })],
    })
    expect(ids(got)).toEqual(["node:notready:nod_1"])
    expect(got[0].tone).toBe("danger")
    expect(got[0].title).toContain("disk pressure")
  })

  it("says both when a node is cordoned and down", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      nodes: [node({ ready: false, cordoned: true })],
    })
    expect(ids(got).sort()).toEqual([
      "node:cordoned:nod_1",
      "node:notready:nod_1",
    ])
  })

  it("leaves a rollout that is still inside its settling window alone", () => {
    // A rollout in flight is not a thing to do.
    const got = deriveClusterActionables({
      cluster: cluster(),
      deployments: [
        deployment({
          replicas: replicas({ ready: 0, pending: 2, failed: 0 }),
          admission_phase: "placing",
          placed_at: new Date(NOW.getTime() - 2 * MINUTES).toISOString(),
        }),
      ],
    })
    expect(got).toEqual([])
  })

  it("flags a rollout that has been mid-flight too long", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      deployments: [
        deployment({
          replicas: replicas({ ready: 0, pending: 2, failed: 0 }),
          admission_phase: "pending",
          admission_reason: "no cluster has capacity",
          placed_at: new Date(NOW.getTime() - 20 * MINUTES).toISOString(),
        }),
      ],
    })
    expect(ids(got)).toEqual(["deployment:phase:dep_1"])
    expect(got[0].detail).toBe("no cluster has capacity")
  })

  it("says the phase or the shortfall, never both while placement is running", () => {
    // They are the same fact told twice.
    const got = deriveClusterActionables({
      cluster: cluster(),
      deployments: [
        deployment({
          replicas: replicas({ ready: 0, pending: 2, failed: 0 }),
          admission_phase: "placing",
          placed_at: new Date(NOW.getTime() - 20 * MINUTES).toISOString(),
        }),
      ],
    })
    expect(got).toHaveLength(1)
    expect(got[0].id).toBe("deployment:phase:dep_1")
  })

  it("flags a settled deployment that is short of replicas", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      deployments: [
        deployment({ replicas: replicas({ ready: 1, pending: 0, failed: 1 }) }),
      ],
    })
    expect(ids(got)).toEqual(["deployment:replicas:dep_1"])
    expect(got[0].tone).toBe("warn")
    expect(got[0].detail).toContain("1 failed")
  })

  it("calls a settled deployment with no replicas at all a failure", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      deployments: [
        deployment({ replicas: replicas({ ready: 0, pending: 0, failed: 2 }) }),
      ],
    })
    expect(got[0].tone).toBe("danger")
  })

  it("says nothing about a deployment that is switched off", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      deployments: [
        deployment({
          enabled: false,
          replicas: replicas({ ready: 0, pending: 0, failed: 0 }),
        }),
      ],
    })
    expect(got).toEqual([])
  })

  it("says nothing about a deployment scaled to zero on purpose", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      deployments: [
        deployment({
          desired_replicas: 0,
          replicas: replicas({ ready: 0, pending: 0, failed: 0 }),
        }),
      ],
    })
    expect(got).toEqual([])
  })

  it("orders rows by how loud they are", () => {
    const got = deriveClusterActionables({
      cluster: cluster(),
      nodes: [node({ id: "nod_c", cordoned: true })],
      devices: [
        device({ id: "dev_warm", health: "degraded" }),
        device({ id: "dev_bad", health: "unhealthy" }),
      ],
    })
    expect(got.map((r) => r.tone)).toEqual(["danger", "warn", "info"])
  })

  it("costs a failed sub-query its own rows and not the whole list", () => {
    // Each source is optional, so one broken fetch does not blank the panel.
    const got = deriveClusterActionables({
      cluster: cluster(),
      devices: [device({ id: "dev_bad", health: "unhealthy" })],
    })
    expect(ids(got)).toEqual(["device:dev_bad"])
  })

  it("gives every row a stable, unique id so React can key on it", () => {
    const got = deriveClusterActionables({
      cluster: cluster({ sync: sync({ state: "disconnected" }) }),
      pools: [
        pool({ min_nodes: 9, membership: { nodes_total: 1 } } as Partial<Pool>),
      ],
      nodes: [node({ ready: false }), node({ id: "nod_2", cordoned: true })],
      devices: [device({ id: "dev_bad", health: "unhealthy" })],
      deployments: [
        deployment({ replicas: replicas({ ready: 0, pending: 0, failed: 2 }) }),
      ],
      operations: [
        {
          id: "opr_1",
          type: "node.drain",
          phase: "running",
          attempts: 1,
          created_at: NOW.toISOString(),
          target: { type: "node", id: "nod_1" },
        } as Operation,
      ],
    })
    expect(new Set(ids(got)).size).toBe(got.length)
    expect(got.length).toBeGreaterThan(4)
  })
})
