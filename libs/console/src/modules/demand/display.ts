import type { StatusTone } from "@/components/layout/status-dot"

const WORKLOAD_PHASE_TONES: Record<string, StatusTone> = {
  pending: "neutral",
  admitted: "info",
  placed: "info",
  running: "ok",
  failed: "danger",
}

export function workloadPhaseTone(phase: string): StatusTone {
  return WORKLOAD_PHASE_TONES[phase] ?? "warn"
}

export const WORKLOAD_PHASES = ["pending", "admitted", "placed", "running"]
export const WORKLOAD_TYPES = ["service", "task", "gang"]

export function formatBytes(bytes: number): string {
  if (bytes <= 0) return "—"
  const units = ["B", "KiB", "MiB", "GiB", "TiB"]
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`
}

export function formatParameters(count: number | null): string {
  if (count === null) return "—"
  if (count >= 1e9) return `${(count / 1e9).toFixed(1)}B`
  if (count >= 1e6) return `${(count / 1e6).toFixed(0)}M`
  return String(count)
}

/** `tp8/pp1/ep1`, the shorthand every engine log prints. */
export function parallelismLabel(p: {
  tensor: number
  pipeline: number
  expert: number
}): string {
  return `tp${p.tensor}/pp${p.pipeline}/ep${p.expert}`
}

/**
 * The steps a deployment passes through between being declared and serving, and
 * which one an observed `(phase, reason)` places it at.
 *
 * The reasons are the operator's closed set (`MESSAGES.md`). They are mapped here
 * rather than rendered raw because a person watching a deployment come up wants to
 * know how far along it is, and `WeightsNotResident` answers that only if you
 * already know what the operator does in what order.
 */
export const LIFECYCLE_STEPS = [
  { key: "declared", label: "Declared" },
  { key: "accepted", label: "Accepted" },
  { key: "dependencies", label: "Recipe & weights" },
  { key: "capacity", label: "Capacity" },
  { key: "rendering", label: "Rendering" },
  { key: "applied", label: "Applied" },
  { key: "serving", label: "Serving" },
] as const

export type LifecycleStep = (typeof LIFECYCLE_STEPS)[number]["key"]

const REASON_STEPS: Record<string, LifecycleStep> = {
  Accepted: "accepted",
  RecipeNotDeclared: "dependencies",
  ArtifactNotDeclared: "dependencies",
  WeightsNotResident: "dependencies",
  WarmSetIncomplete: "dependencies",
  NoSchedulableCapacity: "capacity",
  Rendering: "rendering",
  Applied: "applied",
  Progressing: "serving",
  Serving: "serving",
  PayloadUndecodable: "declared",
  RenderFailed: "rendering",
  NoClusterWork: "declared",
}

/**
 * Where a phase alone puts the strip, for a `reason` this console has never heard of.
 *
 * The fallback is the phase and not `declared`. A cluster runs its own build and may
 * report a sub-state that postdates this console, and answering "declared" for it
 * would walk a serving deployment back to the first step — worse than the raw name,
 * which is at least true. The phase is the field the contract says to compute on.
 */
const PHASE_STEPS: Record<string, LifecycleStep> = {
  pending: "accepted",
  admitted: "accepted",
  placed: "applied",
  degraded: "serving",
  running: "serving",
  terminating: "serving",
  failed: "declared",
}

/**
 * Both halves, because neither answers alone.
 *
 * `reason` is the precise one and is preferred, but it goes stale in one direction the
 * phase does not: everything the operator reports after applying used to carry
 * `Applied`, so a reason-only reading pinned a serving deployment to "Applied" while
 * the counts underneath it said otherwise.
 */
export function lifecycleStepFor(
  phase: string,
  reason: string | null | undefined
): LifecycleStep {
  const byReason = reason ? REASON_STEPS[reason] : undefined
  return byReason ?? PHASE_STEPS[phase] ?? "declared"
}

/**
 * How a `reason` reads to somebody who has not read the operator's source.
 *
 * Falls back to the reason itself, which is deliberately not "Unknown": the raw
 * name is what they will search the operator logs for.
 */
const REASON_TEXT: Record<string, string> = {
  Accepted: "The cluster has the instruction and is working on it",
  RecipeNotDeclared: "Waiting for the recipe to reach the cluster",
  ArtifactNotDeclared: "Waiting for the artifact to reach the cluster",
  WeightsNotResident: "Downloading the weights onto the cluster",
  WarmSetIncomplete: "Weights are still being fetched",
  NoSchedulableCapacity: "No node has enough accelerators free",
  Rendering: "Building the objects that will run this",
  Applied: "Applied to the cluster; waiting for the first replica",
  Progressing: "Replicas are still coming up",
  Serving: "Every replica is ready",
  PayloadUndecodable: "The cluster could not read this deployment",
  RenderFailed: "The cluster could not turn this into anything runnable",
  NoClusterWork: "Held as declared state — authority is observed",
}

export function reasonText(reason: string): string {
  return REASON_TEXT[reason] ?? reason
}
