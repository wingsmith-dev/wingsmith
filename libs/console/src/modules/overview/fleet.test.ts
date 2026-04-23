import type { Cluster } from "@/modules/clusters/types"
import type { Deployment, Endpoint } from "@/modules/demand/types"
import type { DiscoveredWorkload } from "@/modules/discovered/types"
import type { Device } from "@/modules/supply/types"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { deriveFleetAttention } from "./attention"
import {
  allocationTotals,
  capacityByClass,
  reachabilityRows,
  replicaFootprint,
  staleClusters,
  stuckDeployments,
  summariseEndpointHealth,
  unschedulableReplicas,
} from "./fleet"

const NOW = new Date("2026-08-27T12:00:00Z")
const MINUTES = 60 * 1000

// Factories carry only what a test asserts on; casting is deliberate, the way
// summary.test.ts does it — restating forty wire fields would hide the one field
// that matters to the assertion.
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

function deployment(over: Partial<Deployment> = {}): Deployment {
  return {
    id: "dep_1",
    name: "llama-iad",
    workload_name: "llama",
    cluster_id: "clu_1",
    desired_replicas: 2,
    admission_phase: "running",
    admission_reason: null,
    enabled: true,
    queued_at: NOW.toISOString(),
    admitted_at: NOW.toISOString(),
    placed_at: NOW.toISOString(),
    replicas: replicas(),
    status: null,
    ...over,
  } as Deployment
}

function endpoint(over: Partial<Endpoint> = {}): Endpoint {
  return {
    id: "end_1",
    name: "chat",
    enabled: true,
    routing_strategy: "weighted",
    targets: [
      { deployment_id: "dep_1", weight: 100, role: "primary", enabled: true },
    ],
    ...over,
  } as Endpoint
}

function device(over: Partial<Device> = {}): Device {
  return {
    id: "dev_1",
    cluster: "clu_1",
    node_name: "iad-1-n01",
    device_index: 0,
    device_class: "h100-80gb-sxm",
    health: "healthy",
    allocatable: true,
    throttled: false,
    throttle_reasons: [],
    ...over,
  } as Device
}

function cluster(over: Partial<Cluster> = {}): Cluster {
  return {
    id: "clu_1",
    name: "iad-1",
    display_name: null,
    phase: "active",
    reachable: true,
    sync: {
      state: "connected",
      last_sync_at: NOW.toISOString(),
      last_seen_at: NOW.toISOString(),
      last_error: null,
      agent_version: "1.4.0",
    },
    ...over,
  } as Cluster
}

function sighting(over: Partial<DiscoveredWorkload> = {}): DiscoveredWorkload {
  return {
    id: "dsc_1",
    cluster_name: "iad-1",
    namespace: "inference-legacy",
    name: "qwen2.5-72b",
    kind: "deployment",
    replicas_desired: 4,
    replicas_ready: 4,
    devices_total: 8,
    status: "new",
    first_seen_at: NOW.toISOString(),
    ...over,
  } as DiscoveredWorkload
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})

afterEach(() => {
  vi.useRealTimers()
})

describe("summariseEndpointHealth", () => {
  it("reads an endpoint's health off the deployments its targets name", () => {
    const summary = summariseEndpointHealth(
      [
        endpoint({ id: "end_ok", targets: [target("dep_ok")] }),
        endpoint({ id: "end_part", targets: [target("dep_part")] }),
        endpoint({ id: "end_down", targets: [target("dep_down")] }),
      ],
      [
        deployment({ id: "dep_ok" }),
        deployment({ id: "dep_part", replicas: replicas({ ready: 1 }) }),
        deployment({ id: "dep_down", replicas: replicas({ ready: 0 }) }),
      ]
    )

    expect(summary.serving).toBe(1)
    expect(summary.degraded).toBe(1)
    expect(summary.down).toBe(1)
  })

  it("counts an endpoint with nothing to point at as idle, not as down", () => {
    const summary = summariseEndpointHealth([endpoint({ targets: [] })], [])
    expect(summary.idle).toBe(1)
    expect(summary.down).toBe(0)
  })

  it("leaves disabled endpoints out entirely", () => {
    const summary = summariseEndpointHealth([endpoint({ enabled: false })], [])
    expect(summary.rows).toHaveLength(0)
  })
})

function target(id: string): Endpoint["targets"][number] {
  return { deployment_id: id, weight: 100, role: "primary", enabled: true }
}

describe("stuckDeployments", () => {
  it("leaves a rollout that has only just started alone", () => {
    const fresh = deployment({
      admission_phase: "pending",
      queued_at: new Date(NOW.getTime() - 2 * MINUTES).toISOString(),
      admitted_at: null,
      placed_at: null,
    })
    expect(stuckDeployments([fresh])).toHaveLength(0)
  })

  it("counts the replicas a wedged deployment never got", () => {
    const wedged = deployment({
      admission_phase: "pending",
      desired_replicas: 4,
      replicas: replicas({ ready: 1 }),
      queued_at: new Date(NOW.getTime() - 20 * MINUTES).toISOString(),
      admitted_at: null,
      placed_at: null,
    })
    expect(unschedulableReplicas([wedged])).toBe(3)
  })

  it("treats a deployment with no timestamps as stuck rather than as settling", () => {
    const undated = deployment({
      admission_phase: "pending",
      queued_at: null,
      admitted_at: null,
      placed_at: null,
    })
    expect(stuckDeployments([undated])).toHaveLength(1)
  })
})

describe("capacity", () => {
  it("reads allocated as the devices that are no longer allocatable", () => {
    const rows = capacityByClass([
      device({ id: "dev_1", allocatable: false }),
      device({ id: "dev_2", allocatable: false }),
      device({ id: "dev_3", allocatable: true }),
      device({ id: "dev_4", device_class: "l40s-48gb", allocatable: true }),
    ])

    expect(rows[0]).toEqual({
      deviceClass: "h100-80gb-sxm",
      total: 3,
      allocated: 2,
      percent: 67,
    })
    expect(allocationTotals([device({ allocatable: false })])).toEqual({
      total: 1,
      allocated: 1,
      percent: 100,
    })
  })

  it("orders the tightest accelerator first", () => {
    const rows = capacityByClass([
      device({ id: "dev_1", device_class: "l40s-48gb", allocatable: true }),
      device({
        id: "dev_2",
        device_class: "h100-80gb-sxm",
        allocatable: false,
      }),
    ])
    expect(rows.map((row) => row.deviceClass)).toEqual([
      "h100-80gb-sxm",
      "l40s-48gb",
    ])
  })
})

describe("reachability", () => {
  it("names a cluster that never dialled in as not enrolled rather than stale", () => {
    const rows = reachabilityRows([
      cluster({
        sync: {
          ...cluster().sync,
          state: "never_connected",
          last_sync_at: null,
        },
      }),
    ])
    expect(rows[0].note).toBe("not enrolled")
    expect(rows[0].stale).toBe(false)
    expect(staleClusters([cluster()])).toHaveLength(0)
  })

  it("caveats the page with every cluster whose link is degraded", () => {
    const dropped = cluster({ id: "clu_2", reachable: false })
    expect(staleClusters([cluster(), dropped])).toEqual([dropped])
  })
})

describe("replicaFootprint", () => {
  it("counts only the clusters actually carrying a ready replica", () => {
    expect(
      replicaFootprint([
        deployment({ id: "dep_1", cluster_id: "clu_1" }),
        deployment({
          id: "dep_2",
          cluster_id: "clu_2",
          replicas: replicas({ ready: 0 }),
        }),
      ])
    ).toEqual({ ready: 2, clusters: 1 })
  })
})

describe("deriveFleetAttention", () => {
  it("puts the loudest row first and keeps the discovered half apart", () => {
    const items = deriveFleetAttention({
      clusters: [cluster()],
      endpoints: [endpoint({ targets: [target("dep_down")] })],
      deployments: [
        deployment({ id: "dep_down", replicas: replicas({ ready: 0 }) }),
      ],
      discovered: [sighting()],
    })

    expect(items[0].tone).toBe("danger")
    expect(items[0].kind).toBe("endpoint")
    expect(items.filter((item) => item.group === "discovered")).toHaveLength(1)
    expect(items.at(-1)?.pill).toBe("deployment")
  })

  it("says nothing about a fleet where everything reports and serves", () => {
    expect(
      deriveFleetAttention({
        clusters: [cluster()],
        endpoints: [endpoint()],
        deployments: [deployment()],
        devices: [device()],
        nodes: [],
        discovered: [],
      })
    ).toEqual([])
  })

  it("files one placement row for the whole fleet, not one per deployment", () => {
    const wedged = (id: string): Deployment =>
      deployment({
        id,
        admission_phase: "pending",
        admission_reason: "no h100 capacity",
        desired_replicas: 2,
        replicas: replicas({ ready: 0 }),
        queued_at: new Date(NOW.getTime() - 30 * MINUTES).toISOString(),
        admitted_at: null,
        placed_at: null,
      })

    const items = deriveFleetAttention({
      deployments: [wedged("dep_1"), wedged("dep_2")],
    })

    const placement = items.filter((item) => item.kind === "allocation")
    expect(placement).toHaveLength(1)
    expect(placement[0].title).toBe("4 replicas unschedulable")
    expect(placement[0].detail).toContain("no h100 capacity")
  })
})
