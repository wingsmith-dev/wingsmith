import type {
  DeploymentRoleObserved,
  DeploymentStatus,
  ServingRole,
  Workload,
  WorkloadDeployment,
  WorkloadRole,
} from "@/modules/demand/types"

/**
 * Everything the console derives about a disaggregated workload, in one file.
 *
 * It is one file because none of it is stored. The declared ratio is two
 * weights, the achieved ratio is a quotient of observed counts, and the group's
 * device cost is a sum — no field behind any of them. Spreading three
 * derivations of one subject across three components is how two of them end up
 * disagreeing about what "ready" means, which is the exact failure this feature
 * exists to make visible.
 */

/** Prefill first, decode second, anything else after. The order a person reads. */
const ROLE_ORDER: Record<string, number> = { prefill: 0, decode: 1 }

export function roleOrder(role: ServingRole): number {
  return ROLE_ORDER[role] ?? 9
}

function byRole<T extends { role: ServingRole }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => roleOrder(a.role) - roleOrder(b.role))
}

/**
 * Absent `topology` is `aggregated`, and that is not a guess.
 *
 * A plane that predates this feature sends no key, and every workload it knows
 * about is aggregated by definition. Treating absent as unknown would make the
 * console render a third state the object model does not have.
 */
export function isDisaggregated(workload: {
  serving: { topology?: { serving_topology: string } }
}): boolean {
  return workload.serving.topology?.serving_topology === "disaggregated"
}

/** The declared roles, prefill first, or an empty list on an aggregated workload. */
export function declaredRoles(workload: Workload): WorkloadRole[] {
  if (!isDisaggregated(workload)) return []
  return byRole(workload.serving.topology?.roles ?? [])
}

/**
 * `1 prefill : 2 decode`, spelled out.
 *
 * Not `1:2`, which needs the reader to already know which end is which, and not
 * `1P:2D`, which is the recipe's shorthand rather than anything this console
 * has taught anybody. Null when there is no ratio to state.
 */
export function declaredRatio(workload: Workload): string | null {
  const roles = declaredRoles(workload)
  if (roles.length === 0) return null
  return roles.map((role) => `${role.weight} ${role.role}`).join(" : ")
}

/**
 * The devices one group costs.
 *
 * Reported by the plane as `group_devices` when it sends it; otherwise summed
 * here from `Σ weight × device_count`. `device_count` is devices per WORKER — 8
 * at TP8 — and must stay that, because it is a digest field and the group's 24
 * is not. This function is the only place the two are allowed to meet.
 */
export function groupDevices(workload: Workload): number | null {
  if (!isDisaggregated(workload)) return null
  const reported = workload.serving.topology?.group_devices
  if (typeof reported === "number" && reported > 0) return reported
  const roles = declaredRoles(workload)
  if (roles.length === 0) return null
  const per = workload.serving.shape.device_count
  return roles.reduce((total, role) => total + role.weight * per, 0)
}

/** Devices one role costs inside a group. */
export function roleDevices(workload: Workload, role: WorkloadRole): number {
  return role.weight * workload.serving.shape.device_count
}

export interface RoleReadiness {
  role: ServingRole
  ready: number
  desired: number
  /** Every worker this role wants is up. */
  complete: boolean
  /**
   * The role is up in no numbers at all while something is expected of it. A
   * disaggregated deployment with no prefill worker does not serve degraded; it
   * serves nothing, because there is no process to run a prefill and every
   * request fails. This is the one per-role fact that is loud.
   */
  absent: boolean
}

/**
 * Per-role readiness, or null when the cluster reports none.
 *
 * Null and not an empty array: an aggregated deployment and an operator too old
 * to send roles both report nothing here, and in both cases the right thing to
 * render is the scalar rollup that has always been there rather than a per-role
 * view of no roles.
 */
export function roleReadiness(
  status: DeploymentStatus | null | undefined
): RoleReadiness[] | null {
  const roles = status?.roles
  if (!roles || roles.length === 0) return null
  return byRole(roles).map((observed) => ({
    role: observed.role,
    ready: observed.ready,
    desired: observed.desired,
    complete: observed.ready >= observed.desired,
    absent: observed.ready === 0 && observed.desired > 0,
  }))
}

/** `1/1 prefill · 2/2 decode`, never `3 of 3`. */
export function roleReadinessLabel(roles: RoleReadiness[]): string {
  return roles
    .map((role) => `${role.ready}/${role.desired} ${role.role}`)
    .join(" · ")
}

/**
 * A deployment is ready only when EVERY role is.
 *
 * The scalar rollup cannot answer this. Under a rendered disaggregated graph the
 * old single count compares decode pods to a group count — two numbers that are
 * not comparable — and it can read `ready` on a deployment with no prefill
 * worker at all. Where roles are reported they are the answer and the scalar is
 * not consulted.
 */
export function rolesReady(roles: RoleReadiness[]): boolean {
  return roles.every((role) => role.complete)
}

export interface AchievedRatio {
  /** `1 prefill : 1.5 decode` — the quotient, at the same scale as declared. */
  label: string
  /** The observed counts it was derived from, prefill first. */
  counts: DeploymentRoleObserved[]
  /** The quotient equals the declared one. */
  matchesDeclared: boolean
  /**
   * True when the two disagree AND that disagreement is a deployment part way
   * through a scale rather than a fault. Everything this console can see says
   * settling: the counts are legitimately allowed to disagree while roles come
   * up at different speeds, and §6.3 is explicit that achieved is a quotient
   * and not a stored field, so it has no authority to be wrong against.
   */
  settling: boolean
}

/**
 * The achieved ratio, as a quotient of observed per-role counts.
 *
 * Nothing on the wire carries this and nothing should. It is derived at read
 * time from counts ingestion already writes, which is what keeps ingestion the
 * single writer of observed state — a stored achieved ratio would be a second
 * path to the same fact and the two would drift.
 *
 * `ready` and not `desired` is the numerator, deliberately. `desired` is the
 * plane's own number reflected back and its quotient is the declared ratio by
 * construction, which is a reading that can never disagree and therefore never
 * says anything.
 */
export function achievedRatio(
  workload: Workload,
  status: DeploymentStatus | null | undefined
): AchievedRatio | null {
  const observed = status?.roles
  if (!observed || observed.length === 0) return null
  const counts = byRole(observed)
  if (counts.every((count) => count.ready === 0)) return null

  const base = counts[0].ready
  const label = counts
    .map((count) => `${formatQuotient(count.ready / base)} ${count.role}`)
    .join(" : ")

  const declared = declaredRoles(workload)
  const declaredBase = declared[0]?.weight ?? 0
  const matchesDeclared =
    declared.length === counts.length &&
    declaredBase > 0 &&
    counts.every((count, i) => {
      const want = declared[i]
      return (
        want?.role === count.role &&
        Math.abs(count.ready / base - want.weight / declaredBase) < 1e-9
      )
    })

  return {
    label,
    counts,
    matchesDeclared,
    settling: !matchesDeclared,
  }
}

/**
 * One decimal place, and no trailing zero.
 *
 * A ratio is read, not measured. `1.5` is the answer; `1.50` implies a precision
 * a count of three pods does not have, and `1.3333333333333333` is not a number
 * anybody can compare against a `1 : 2` printed above it.
 */
function formatQuotient(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

/**
 * What the scale numbers count for THIS workload.
 *
 * `scale.unit` is named by kind and says `replicas`, which stops being true the
 * moment a workload is disaggregated: `min_replicas` keeps meaning "this
 * cluster's share" and starts meaning a share of GROUPS, one group being the
 * per-role weights. Rendering `3 replicas` over nine processes is the
 * reinterpretation this feature has to make impossible to walk into.
 */
export function scaleUnitFor(workload: Workload): string {
  return isDisaggregated(workload) ? "groups" : workload.scale.unit
}

/**
 * The per-role process counts a group count multiplies out to.
 *
 * The plane does this multiplication and sends absolute counts; this is the
 * same arithmetic for a form that is asking what a number WOULD mean before
 * anybody commits it.
 */
export function rolesAtGroupCount(
  workload: Workload,
  groups: number
): { role: ServingRole; count: number }[] {
  return declaredRoles(workload).map((role) => ({
    role: role.role,
    count: groups * role.weight,
  }))
}

/**
 * Whether a role's declared configuration differs from the workload's own.
 *
 * Only what a role overrides is worth showing per role; repeating the fields it
 * inherits under each role is how somebody comes to believe a role declares a
 * value it does not, and then edits it there.
 */
export function roleOverrides(
  role: WorkloadRole
): { key: string; value: string }[] {
  const rows = Object.entries(role.engine_args ?? {}).map(([key, value]) => ({
    key,
    value: String(value === "" ? "(set)" : value),
  }))
  if (role.max_batch_tokens !== null && role.max_batch_tokens !== undefined) {
    rows.unshift({
      key: "max-num-batched-tokens",
      value: role.max_batch_tokens.toLocaleString(),
    })
  }
  return rows.sort((a, b) => a.key.localeCompare(b.key))
}

/**
 * The roles the console will not let anybody type an argument into.
 *
 * `disaggregation-mode` is the role itself and `kv-transfer-config` is the same
 * string on both roles — the transfer mechanism is a property of the topology
 * and not of a role. A value for either arriving from a role's `engine_args` is
 * not a default, it is a contradiction of a decision already acted on, and the
 * plane refuses it. Refusing it in the form as well means the person finds out
 * while they can still fix it.
 */
export const ROLE_RESERVED_ARGS = [
  "disaggregation-mode",
  "kv-transfer-config",
] as const

export function reservedRoleArgs(args: Record<string, unknown>): string[] {
  return Object.keys(args).filter((key) =>
    (ROLE_RESERVED_ARGS as readonly string[]).includes(key.replace(/^--/, ""))
  )
}

/** The deployments of a workload that report per-role counts at all. */
export function deploymentsWithRoles(
  deployments: WorkloadDeployment[]
): WorkloadDeployment[] {
  return deployments.filter((d) => (d.status?.roles?.length ?? 0) > 0)
}
