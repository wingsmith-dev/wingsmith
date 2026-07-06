import { describe, expect, it } from "vitest"

import {
  achievedRatio,
  declaredRatio,
  groupDevices,
  isDisaggregated,
  refusedRoleArgs,
  rolloutCost,
  strandedByRatio,
  roleWorkers,
  recipeRoles,
  formatMinutes,
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
      refusedRoleArgs({
        "disaggregation-mode": "prefill",
        "--kv-transfer-config": "{}",
        "max-num-seqs": "4",
      })
    ).toEqual(["disaggregation-mode", "--kv-transfer-config"])
  })

  it("refuses a derived flag that has a typed field of its own", () => {
    // `max-num-batched-tokens` is set through `max_batch_tokens` beside the
    // box, not inside it. The plane rejects it either way.
    expect(refusedRoleArgs({ "max-num-batched-tokens": "2048" })).toEqual([
      "max-num-batched-tokens",
    ])
    expect(refusedRoleArgs({ "tensor-parallel-size": "8" })).toEqual([
      "tensor-parallel-size",
    ])
  })
})

describe("what a per-role configuration change costs", () => {
  it("multiplies the weights by the group count for every role", () => {
    // The same product the plane sends on the wire. Both are declared, so this
    // is exact rather than observed.
    expect(roleWorkers(disagg())).toEqual([
      { role: "prefill", weight: 1, workers: 2 },
      { role: "decode", weight: 2, workers: 4 },
    ])
  })

  it("counts every worker, not just the role being edited", () => {
    // Dynamo hashes one worker spec across the whole graph, so an edit to
    // either role renames both roles' objects.
    const cost = rolloutCost(disagg())!
    expect(cost.workers).toBe(6)
  })

  it("takes the longest role rather than the sum", () => {
    // Each role is a Deployment of its own; they are not made to wait for one
    // another. Four decode at twenty minutes each is the critical path.
    expect(rolloutCost(disagg())!.minutes).toBe(80)
  })

  it("keeps serving when every role has a worker to spare", () => {
    const cost = rolloutCost(disagg())!
    expect(cost.servesThrough).toBe(true)
    expect(cost.roleAtRisk).toBeNull()
  })

  it("stops serving when a role has exactly one worker", () => {
    // Rolling is in place, one worker at a time, so a role of one goes to zero
    // while its only worker reloads — and a decode worker with no prefill peer
    // has nothing to decode from. This is the outage case.
    const single = workload({
      scale: {
        unit: "replicas",
        min: 1,
        max: 1,
        desired: 1,
        placed: 1,
        ready: 1,
      },
      serving: {
        shape: { device_count: 8 },
        topology: {
          serving_topology: "disaggregated",
          roles: ROLES,
          group_devices: 0,
        },
      },
    } as unknown as Partial<Workload>)
    const cost = rolloutCost(single)!
    expect(cost.servesThrough).toBe(false)
    expect(cost.roleAtRisk).toBe("prefill")
    expect(cost.minutes).toBe(40)
  })

  it("reads long rolls in hours", () => {
    expect(formatMinutes(40)).toBe("40 min")
    expect(formatMinutes(120)).toBe("2 h")
    expect(formatMinutes(80)).toBe("1 h 20 min")
  })
})

describe("a ratio that does not divide the hardware", () => {
  it("says nothing when the group divides the budget", () => {
    expect(strandedByRatio(48, 24)).toBeNull()
    expect(strandedByRatio(24, 24)).toBeNull()
  })

  it("names the idle devices when it does not", () => {
    // Four nodes cannot express 1:2 at TP8 — three workers is 24 devices, and
    // 32 leaves a whole node doing nothing. Nothing fails, which is the problem.
    expect(strandedByRatio(32, 24)).toBe(
      "a unit of 24 devices does not divide 32: 1 unit fits and 8 devices would sit idle"
    )
  })

  it("says so plainly when not even one unit fits", () => {
    expect(strandedByRatio(16, 24)).toBe(
      "a unit of 24 devices does not fit 16: no unit fits and all 16 would sit idle"
    )
  })

  it("is silent without a budget rather than guessing at one", () => {
    expect(strandedByRatio(0, 24)).toBeNull()
    expect(strandedByRatio(32, 0)).toBeNull()
  })
})

describe("the starting point for a new disaggregated workload", () => {
  it("lands the recipe's own numbers, ready to submit unedited", () => {
    // Choosing disaggregated is one click. The sixteenfold batch-token split
    // between the roles is the recipe's, not a preference this console invented.
    const roles = recipeRoles()
    expect(roles.map((role) => role.role)).toEqual(["prefill", "decode"])
    expect(roles[0].max_batch_tokens).toBe(32_768)
    expect(roles[1].max_batch_tokens).toBe(2048)
    expect(roles[1].weight).toBe(2)
  })

  it("hands out its own copy, so editing one workload cannot edit the default", () => {
    const first = recipeRoles()
    first[0].engine_args["max-num-seqs"] = "999"
    expect(recipeRoles()[0].engine_args["max-num-seqs"]).toBe("4")
  })
})
