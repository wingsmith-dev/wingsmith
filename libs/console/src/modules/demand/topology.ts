import { SERVING_ROLES } from "@/modules/demand/types"
import type {
  DeploymentRoleObserved,
  DeploymentStatus,
  ServingRole,
  Workload,
  WorkloadRole,
} from "@/modules/demand/types"

/**
 * Everything the console derives about a disaggregated workload, in one file.
 *
 * It is one file because none of it is stored. The declared ratio is two
 * weights, the achieved ratio is a quotient of observed counts, and the group's
 * device cost is a sum: no field behind any of them. Spreading three
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
 * `1P:2D`, which is the recipe's shorthand instead of anything this console
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
 * here from `Σ weight × device_count`. `device_count` is devices per WORKER (8
 * at TP8) and must stay that, because it is a digest field and the group's 24
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
 * render is the scalar rollup that has always been there instead of a per-role
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

/**
 * A deployment is ready only when EVERY role is.
 *
 * The scalar rollup cannot answer this. Under a rendered disaggregated graph the
 * old single count compares decode pods to a group count (two numbers that are
 * not comparable), and it can read `ready` on a deployment with no prefill
 * worker at all. Where roles are reported they are the answer and the scalar is
 * not consulted.
 */
export function rolesReady(roles: RoleReadiness[]): boolean {
  return roles.every((role) => role.complete)
}

export interface AchievedRatio {
  /** `1 prefill : 1.5 decode`: the quotient, at the same scale as declared. */
  label: string
  /** The observed counts it was derived from, prefill first. */
  counts: DeploymentRoleObserved[]
  /** The quotient equals the declared one. */
  matchesDeclared: boolean
  /**
   * True when the two disagree AND that disagreement is a deployment part way
   * through a scale instead of a fault. Everything this console can see says
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
 * single writer of observed state: a stored achieved ratio would be a second
 * path to the same fact and the two would drift.
 *
 * `ready` and not `desired` is the numerator, by design. `desired` is the
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
  const rows = Object.entries(role.engine_args).map(([key, value]) => ({
    key,
    value: String(value === "" ? "(set)" : value),
  }))
  if (role.max_batch_tokens !== null) {
    rows.unshift({
      key: "max-num-batched-tokens",
      value: role.max_batch_tokens.toLocaleString(),
    })
  }
  return rows.sort((a, b) => a.key.localeCompare(b.key))
}

/**
 * The arguments a role may not carry, mirroring the plane's own two sets.
 *
 * `DERIVED_FLAGS` is rendered from a column somebody filled in, and
 * `TOPOLOGY_FLAGS` from the topology and the role: `disaggregation-mode` is
 * the role itself, and `kv-transfer-config` is the same string on both roles
 * because the transfer mechanism belongs to the topology. A value for either
 * from a role's `engine_args` is not a default, it is a contradiction of a
 * decision already acted on, and the plane refuses it.
 *
 * Checked here as well so the person finds out while the form is still open. A
 * refusal that arrives from the server is the same refusal a minute later with
 * the reason detached from the field that caused it.
 *
 * `max-num-batched-tokens` is on this list and is NOT an omission: it is a
 * derived flag with a typed field of its own, so it is set through
 * `max_batch_tokens` beside the argument box, not inside it.
 */
export const ROLE_REFUSED_ARGS = [
  "model",
  "served-model-name",
  "revision",
  "max-model-len",
  "tensor-parallel-size",
  "pipeline-parallel-size",
  "gpu-memory-utilization",
  "quantization",
  "dtype",
  "kv-cache-dtype",
  "max-num-batched-tokens",
  "no-enable-prefix-caching",
  "disaggregation-mode",
  "kv-transfer-config",
] as const

/** The flag name as the plane keys it: no leading dashes. */
export function flagName(key: string): string {
  return key.replace(/^-+/, "")
}

export function refusedRoleArgs(args: Record<string, unknown>): string[] {
  return Object.keys(args).filter((key) =>
    (ROLE_REFUSED_ARGS as readonly string[]).includes(flagName(key))
  )
}

export interface RoleWorkers {
  role: ServingRole
  weight: number
  /** `groups × weight`, the same product the plane sends on the wire. */
  workers: number
}

/**
 * Each role's absolute worker count at the workload's current group total.
 *
 * The plane performs this multiplication for the wire and this repeats it for
 * display, which is safe for the reason it is safe there: the group count and
 * the weights are both declared, so the product is exact, not observed.
 * It is emphatically not a readiness figure: nothing here says a worker is up.
 */
export function roleWorkers(workload: Workload): RoleWorkers[] {
  const groups = workload.scale.desired
  return declaredRoles(workload).map((role) => ({
    role: role.role,
    weight: role.weight,
    workers: groups * role.weight,
  }))
}

export interface RolloutCost {
  /** Every worker of every role. All of them are replaced. */
  workers: number
  perRole: RoleWorkers[]
  /**
   * Whether anything answers a request for the whole of the roll.
   *
   * Rolling happens in place, one worker at a time within a role, so a role of
   * two keeps one worker up throughout and a role of one goes to zero while its
   * only worker reloads. And a decode worker with no prefill peer has nothing
   * to decode from, so one role at zero is the whole model at zero, which
   * makes this the difference between a slow window and an outage.
   */
  servesThrough: boolean
  /** The role that empties, when one does. */
  roleAtRisk: ServingRole | null
  /**
   * The role with the most workers, which sets how long the roll takes: each
   * role is a Deployment of its own and they roll side by side, but a role's
   * own workers reload one after another. How long one weight load takes
   * depends on the model, the storage and the network, so no time is quoted.
   */
  longest: RoleWorkers
}

/**
 * What a per-role configuration change actually costs this workload.
 *
 * Computed, not quoted. "Three workers reload" is right for 1P:2D at one
 * group and wrong for everything else, and a number that is sometimes wrong is
 * a number people learn to skip past, which is the opposite of what a warning
 * on the destructive path is for.
 *
 * Every worker is counted, not just the edited role's. Dynamo hashes one worker
 * spec across the whole graph, so a change to either role renames both roles'
 * objects. That was observed directly: adding one environment variable to the
 * prefill component produced a new decode Deployment under a new name and a
 * fresh UID.
 */
export function rolloutCost(workload: Workload): RolloutCost | null {
  const perRole = roleWorkers(workload)
  if (perRole.length === 0) return null
  const emptied = perRole.find((role) => role.workers <= 1) ?? null
  const longest = perRole.reduce((most, role) =>
    role.workers > most.workers ? role : most
  )
  return {
    workers: perRole.reduce((total, role) => total + role.workers, 0),
    perRole,
    servesThrough: emptied === null,
    roleAtRisk: emptied?.role ?? null,
    longest,
  }
}

/**
 * What a group does not divide into, in words, or null when there is nothing
 * to say.
 *
 * The plane spells this arithmetic and cannot call it: the sentence needs a
 * device budget, and the declaration path has none: `devices.table()` carries
 * per-device memory and not node shape, so devices-per-node is a fact about a
 * cluster. The console has budgets the plane's declaration path does not, so it
 * says the thing here, in the plane's own words, instead of waiting for a
 * server-side refusal that is not coming.
 *
 * Not a refusal and it must not become one. Nothing fails when a ratio strands
 * a node: that is exactly the problem, and why it is worth saying where the
 * ratio is being chosen instead of leaving it to be discovered as a bill.
 */
export function strandedByRatio(
  budgetDevices: number,
  groupDevices: number
): string | null {
  if (groupDevices <= 0 || budgetDevices <= 0) return null
  if (budgetDevices % groupDevices === 0) return null
  const fits = Math.floor(budgetDevices / groupDevices)
  const stranded = budgetDevices % groupDevices
  if (fits === 0) {
    return (
      `a unit of ${groupDevices} devices does not fit ${budgetDevices}: ` +
      `no unit fits and all ${budgetDevices} would sit idle`
    )
  }
  return (
    `a unit of ${groupDevices} devices does not divide ${budgetDevices}: ` +
    `${fits} unit${fits === 1 ? " fits" : "s fit"} and ${stranded} device` +
    `${stranded === 1 ? "" : "s"} would sit idle`
  )
}

/**
 * The per-role starting point, from NVIDIA's own recipe.
 *
 * Six arguments are the whole delta between the roles, and these are the ones
 * with numbers in the recipe, not in a preference. They are a default
 * and not a constraint: choosing disaggregated should be one click, and the
 * person who wants to argue with `max-num-seqs` can, in the same form.
 *
 * `max-num-batched-tokens` is a typed field instead of an argument, sixteenfold
 * apart across the two roles, which is why it is here as a number and not a
 * string in the map.
 */
export const RECIPE_ROLE_DEFAULTS: Record<
  string,
  {
    weight: number
    max_batch_tokens: number
    engine_args: Record<string, string>
  }
> = {
  prefill: {
    weight: 1,
    max_batch_tokens: 32_768,
    engine_args: { "max-num-seqs": "4", "enforce-eager": "" },
  },
  decode: {
    weight: 2,
    max_batch_tokens: 2048,
    engine_args: { "max-num-seqs": "256" },
  },
}

/** The two roles as a fresh declaration, ready to submit unedited. */
export function recipeRoles(): WorkloadRole[] {
  return SERVING_ROLES.map((role) => ({
    role,
    weight: RECIPE_ROLE_DEFAULTS[role].weight,
    max_batch_tokens: RECIPE_ROLE_DEFAULTS[role].max_batch_tokens,
    engine_args: { ...RECIPE_ROLE_DEFAULTS[role].engine_args },
  }))
}
