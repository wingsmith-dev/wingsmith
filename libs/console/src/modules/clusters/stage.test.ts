import { describe, expect, it } from "vitest"
import { clusterStage } from "./stage"
import type { Cluster } from "./types"

function sync(over: Partial<Cluster["sync"]> = {}): Cluster["sync"] {
  return {
    state: "connected",
    last_seen_at: null,
    last_sync_at: null,
    last_error: null,
    operator_version: "1.4.0",
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

describe("clusterStage", () => {
  it("is onboarding when the operator has never connected", () => {
    expect(
      clusterStage(
        cluster({ phase: "pending", sync: sync({ state: "never_connected" }) })
      )
    ).toBe("onboarding")
  })

  it("is live when enrolled, reachable and connected", () => {
    expect(clusterStage(cluster())).toBe("live")
  })

  it("is degraded when the link is stale, dropped, or unreachable", () => {
    expect(clusterStage(cluster({ sync: sync({ state: "stale" }) }))).toBe(
      "degraded"
    )
    expect(
      clusterStage(cluster({ sync: sync({ state: "disconnected" }) }))
    ).toBe("degraded")
    expect(clusterStage(cluster({ reachable: false }))).toBe("degraded")
  })

  it("is draining when the phase is draining, over a bad link", () => {
    expect(
      clusterStage(
        cluster({ phase: "draining", sync: sync({ state: "stale" }) })
      )
    ).toBe("draining")
  })

  it("is archived when disabled, even if it never connected", () => {
    expect(
      clusterStage(
        cluster({ phase: "disabled", sync: sync({ state: "never_connected" }) })
      )
    ).toBe("archived")
  })
})
