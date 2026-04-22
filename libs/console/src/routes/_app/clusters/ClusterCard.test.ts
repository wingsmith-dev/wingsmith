import type { Cluster } from "@/modules/clusters/types"
import { describe, expect, it } from "vitest"
import { clusterHealth } from "./ClusterCard"

function sync(over: Partial<Cluster["sync"]> = {}): Cluster["sync"] {
  return {
    state: "connected",
    last_seen_at: null,
    last_sync_at: null,
    last_error: null,
    agent_version: "1.4.0",
    lag_ms: 0,
    ...over,
  } as Cluster["sync"]
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

/**
 * A cluster that never connected reports `reachable: false`, so every case here
 * pairs the two — the pairing is the whole point, and testing `never_connected`
 * against a reachable fixture is what let the card call it a fault.
 */
describe("clusterHealth", () => {
  it("is onboarding when the operator never connected, not down", () => {
    expect(
      clusterHealth(
        cluster({
          phase: "pending",
          reachable: false,
          sync: sync({ state: "never_connected" }),
        })
      )
    ).toBe("onboarding")
  })

  it("is down when a cluster that had enrolled goes unreachable", () => {
    expect(
      clusterHealth(
        cluster({ reachable: false, sync: sync({ state: "disconnected" }) })
      )
    ).toBe("down")
  })

  it("is provisioning for a pending cluster whose operator has connected", () => {
    expect(clusterHealth(cluster({ phase: "pending" }))).toBe("provisioning")
  })

  it("is degraded while draining or on a stale link", () => {
    expect(clusterHealth(cluster({ phase: "draining" }))).toBe("degraded")
    expect(clusterHealth(cluster({ sync: sync({ state: "stale" }) }))).toBe(
      "degraded"
    )
  })

  it("is healthy when enrolled, reachable and connected", () => {
    expect(clusterHealth(cluster())).toBe("healthy")
  })
})
