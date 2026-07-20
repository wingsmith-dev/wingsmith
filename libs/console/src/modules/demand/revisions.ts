import type { WorkloadRevision, WorkloadServingUpdate } from "./types"

/**
 * One field that differs between two serving configurations.
 *
 * `path` is dotted and is the path into the patch body, not a prettified label — `parallelism.tensor`,
 * `cache.kv_cache_fraction`, `engine_args.max-num-seqs`. That is deliberate: a revision is a body
 * somebody can resend, so the name of a change should be the name of the thing they would edit.
 */
export interface ServingChange {
  path: string
  before: string
  after: string
}

/** What a value reads as when the older revision did not carry the field at all. */
export const ABSENT = "—"

/**
 * Every field that differs between two stored serving configurations, older first.
 *
 * Computed here rather than asked of the plane. A revision is stored whole, so the difference
 * between two of them is a fact about two rows this console already holds — and a `changed` field
 * on the wire would be a second, staler answer to a question the client can always compute.
 *
 * Recurses into plain objects, `engine_args` and the overlay included: a flag that moved is
 * exactly the kind of change somebody is looking for, and folding the map into one "engine_args
 * changed" line hides the only part of it that matters. Arrays and scalars are compared by their
 * rendered form, because the blocks replace whole and a half-changed array is not a thing the
 * patch surface can express.
 */
export function servingChanges(
  before: WorkloadServingUpdate | undefined,
  after: WorkloadServingUpdate
): ServingChange[] {
  const from = flatten(before ?? {})
  const to = flatten(after)
  const paths = [...new Set([...Object.keys(from), ...Object.keys(to)])].sort()
  const changes: ServingChange[] = []
  for (const path of paths) {
    const a = from[path] ?? ABSENT
    const b = to[path] ?? ABSENT
    if (a !== b) changes.push({ path, before: a, after: b })
  }
  return changes
}

/**
 * The changes between a revision and the one before it in a newest-first list.
 *
 * The oldest revision has nothing behind it and gets an empty list rather than a diff against
 * `{}` — a `created` or `imported` entry is a starting point, and rendering every field it
 * declares as a change would bury the entries that are ones.
 */
export function changesAgainstPrevious(
  revisions: WorkloadRevision[],
  index: number
): ServingChange[] {
  const previous = revisions[index + 1]
  if (!previous) return []
  return servingChanges(previous.serving, revisions[index].serving)
}

/**
 * Whether a revision is the configuration the workload currently declares.
 *
 * Compared on the digest and never on the number: a workload edited A to B and back to A has two
 * revisions holding one configuration, and both of them are what is declared. Being declared is
 * also not the same as being live — that is counted over replicas, per cluster.
 */
export function isDeclared(
  revision: WorkloadRevision,
  servingDigest: string
): boolean {
  return revision.serving_digest === servingDigest
}

/**
 * Ready replicas per serving digest, summed across every cluster a workload landed on.
 *
 * The only legal summing on this object: each deployment's map counts that cluster's own ready
 * replicas, and a total per configuration is a count of replicas rather than a share measured
 * against a total. Nothing here is compared to `scale.desired`.
 */
export function liveDigests(
  deployments: { live_digests: Record<string, number> }[]
): Record<string, number> {
  const total: Record<string, number> = {}
  for (const deployment of deployments)
    for (const [digest, ready] of Object.entries(deployment.live_digests ?? {}))
      total[digest] = (total[digest] ?? 0) + ready
  return total
}

/** Dotted paths to rendered values, one entry per leaf. */
function flatten(value: unknown, prefix = ""): Record<string, string> {
  if (!isPlainObject(value)) return prefix ? { [prefix]: render(value) } : {}
  const out: Record<string, string> = {}
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (isPlainObject(child)) Object.assign(out, flatten(child, path))
    else out[path] = render(child)
  }
  return out
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  )
}

function render(value: unknown): string {
  if (value === null || value === undefined) return ABSENT
  if (Array.isArray(value)) return value.length ? JSON.stringify(value) : ABSENT
  return String(value)
}
