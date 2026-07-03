import { describe, expect, it } from "vitest"

import {
  achievedRatio,
  declaredRatio,
  groupDevices,
  isDisaggregated,
  reservedRoleArgs,
  roleReadiness,
  roleReadinessLabel,
  rolesAtGroupCount,
  rolesReady,
  scaleUnitFor,
} from "./topology"
import type { DeploymentStatus, Workload } from "./types"

function workload(over: Partial<Workload> = {}): Workload {
  return {
    kind: "service",
    scale: {
      unit: "replicas",
      min: 1,
      max: 4,
      desired: 2,
      placed: 2,
      ready: 2,
    },
    serving: { shape: { device_count: 8 } },
    ...over,
  } as unknown as Workload
}

const ROLES = [
  { role: "decode", weight: 2, engine_args: {}, max_batch_tokens: 2048 },
  { role: "prefill", weight: 1, engine_args: {}, max_batch_tokens: 32_768 },
]

/** Declared 1 prefill : 2 decode, arriving decode-first to pin the ordering. */
const disagg = (topology: Record<string, unknown> = {}) =>
  workload({
    serving: {
      shape: { device_count: 8 },
      topology: {
        serving_topology: "disaggregated",
        roles: ROLES,
        group_devices: 0,
        group_nodes_hint: "",
        ...topology,
      },
    },
  } as unknown as Partial<Workload>)

function status(
  roles: DeploymentStatus["roles"],
  over: Partial<DeploymentStatus> = {}
): DeploymentStatus {
  return {
    observed_generation: 1,
    phase: "running",
    reason: null,
    message: null,
    desired_replicas: 2,
    ready_replicas: 2,
    updated_replicas: 2,
    unavailable_replicas: 0,
    roles,
    last_error_code: null,
    last_error_message: null,
    last_error_at: null,
    observed_at: null,
    ...over,
  }
}

describe("what the topology is", () => {
  it("reads a workload with no topology key as aggregated", () => {
    // A plane that predates the feature sends nothing, and every workload it
    // knows about is aggregated by definition. Absent must not render as a
    // third state the object model does not have.
    expect(isDisaggregated(workload())).toBe(false)
    expect(declaredRatio(workload())).toBeNull()
    expect(groupDevices(workload())).toBeNull()
  })

  it("names the ends of the ratio rather than trusting the reader", () => {
    expect(declaredRatio(disagg())).toBe("1 prefill : 2 decode")
  })

  it("counts groups and not replicas once a workload is disaggregated", () => {
    // `min_replicas` keeps meaning this cluster's share and starts meaning a
    // share of groups. Three of these is nine processes, not three.
    expect(scaleUnitFor(workload())).toBe("replicas")
    expect(scaleUnitFor(disagg())).toBe("groups")
  })
})

describe("what a group costs", () => {
  it("multiplies devices per WORKER by the weights", () => {
    // `device_count` is a digest field and must keep meaning 8 at TP8. The
    // group's 24 is derived here and stored nowhere.
    expect(groupDevices(disagg())).toBe(24)
  })

  it("prefers the number the plane will refuse it in", () => {
    expect(groupDevices(disagg({ group_devices: 40 }))).toBe(40)
  })

  it("multiplies a group count out to per-role processes", () => {
    expect(rolesAtGroupCount(disagg(), 3)).toEqual([
      { role: "prefill", count: 3 },
      { role: "decode", count: 6 },
    ])
  })
})

describe("readiness is per role", () => {
  it("reports nothing where the cluster reports no roles", () => {
    // Aggregated, or an operator too old to send them. Both cases fall back to
    // the scalar rollup, which is right, less precisely.
    expect(roleReadiness(status(undefined))).toBeNull()
    expect(roleReadiness(null)).toBeNull()
  })

  it("puts prefill first whatever order it arrives in", () => {
    const roles = roleReadiness(
      status([
        { role: "decode", desired: 2, ready: 2 },
        { role: "prefill", desired: 1, ready: 1 },
      ])
    )!
    expect(roleReadinessLabel(roles)).toBe("1/1 prefill · 2/2 decode")
    expect(rolesReady(roles)).toBe(true)
  })

  it("is not ready while any one role is short", () => {
    const roles = roleReadiness(
      status([
        { role: "prefill", desired: 2, ready: 1 },
        { role: "decode", desired: 4, ready: 4 },
      ])
    )!
    expect(rolesReady(roles)).toBe(false)
    expect(roles[0].absent).toBe(false)
  })

  it("calls a role at zero absent, however ready the other one is", () => {
    // Every decode worker up and no prefill worker does not serve degraded. It
    // serves nothing: there is no process to run a prefill.
    const roles = roleReadiness(
      status([
        { role: "prefill", desired: 1, ready: 0 },
        { role: "decode", desired: 2, ready: 2 },
      ])
    )!
    expect(roles[0].absent).toBe(true)
    expect(roles[1].absent).toBe(false)
  })
})

describe("the achieved ratio is a quotient", () => {
  it("is absent until something is ready to divide", () => {
    expect(achievedRatio(disagg(), status(undefined))).toBeNull()
    expect(
      achievedRatio(
        disagg(),
        status([
          { role: "prefill", desired: 1, ready: 0 },
          { role: "decode", desired: 2, ready: 0 },
        ])
      )
    ).toBeNull()
  })

  it("agrees with the declared ratio when the counts have arrived", () => {
    const achieved = achievedRatio(
      disagg(),
      status([
        { role: "prefill", desired: 2, ready: 2 },
        { role: "decode", desired: 4, ready: 4 },
      ])
    )!
    expect(achieved.label).toBe("1 prefill : 2 decode")
    expect(achieved.matchesDeclared).toBe(true)
    expect(achieved.settling).toBe(false)
  })

  it("disagrees while a scale is in flight, and that is not a fault", () => {
    // Declared 1:2, observed one prefill against four decode. The quotient is
    // allowed to say 1:4 here; the roles are coming up at different speeds.
    const achieved = achievedRatio(
      disagg(),
      status([
        { role: "prefill", desired: 2, ready: 1 },
        { role: "decode", desired: 4, ready: 4 },
      ])
    )!
    expect(achieved.label).toBe("1 prefill : 4 decode")
    expect(achieved.matchesDeclared).toBe(false)
    expect(achieved.settling).toBe(true)
  })

  it("reads a fraction to one place", () => {
    const achieved = achievedRatio(
      disagg(),
      status([
        { role: "prefill", desired: 2, ready: 2 },
        { role: "decode", desired: 4, ready: 3 },
      ])
    )!
    expect(achieved.label).toBe("1 prefill : 1.5 decode")
  })
})

describe("what a role may not be told", () => {
  it("names the arguments the topology decides, dashed or not", () => {
    // The transfer mechanism is a property of the topology and the mode is the
    // role itself. A value for either from a role's args is a contradiction of
    // a decision already acted on, not a default.
    expect(
      reservedRoleArgs({
        "disaggregation-mode": "prefill",
        "--kv-transfer-config": "{}",
        "max-num-seqs": "4",
      })
    ).toEqual(["disaggregation-mode", "--kv-transfer-config"])
  })
})
