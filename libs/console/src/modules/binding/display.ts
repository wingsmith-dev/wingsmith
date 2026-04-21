import type { LimitScope, OnExceed, ServingQuota } from "./types"

export const LIMIT_SCOPES: LimitScope[] = ["per_replica", "divided"]

export const ON_EXCEED: OnExceed[] = ["reject", "queue"]

/** The only objective the plane accepts; naming an unknown one is refused. */
export const AVAILABILITY_OBJECTIVES = ["availability-2026.1"]

export const LIMIT_SCOPE_LABEL: Record<LimitScope, string> = {
  per_replica: "per replica",
  divided: "divided",
}

/**
 * There is no shared counter across gateway replicas, so one of two error
 * directions has to be chosen, and the choice is spelled out where it is made
 * rather than left to a document nobody opens while editing a commitment.
 */
export const LIMIT_SCOPE_MEANING: Record<LimitScope, string> = {
  per_replica:
    "Every replica enforces the whole number, so a fleet of R admits up to R times what was sold. Errs towards admitting more than the commitment.",
  divided:
    "Each replica enforces its share of the number. An uneven connection balance refuses callers who are inside the limit they were sold. Errs towards refusing traffic that was paid for.",
}

/**
 * A queue timeout only means something while traffic queues. The plane refuses
 * a timeout set beside `reject` rather than storing a number nothing reads, so
 * the form drops it on the way out instead of surfacing that as an error.
 */
export function queueTimeoutFor(
  onExceed: OnExceed,
  ms: number | null
): number | null {
  return onExceed === "queue" ? ms : null
}

export const ON_EXCEED_MEANING: Record<OnExceed, string> = {
  reject: "Traffic over the limit is refused as it arrives.",
  queue:
    "Traffic over the limit waits, and is refused once the queue timeout is up.",
}

/**
 * Grouped digits in en-US, and never compacted. The console is English-only, so
 * the locale is pinned rather than taken from the runtime — and "10M" would hide
 * whether 10,500,000 was what somebody sold.
 */
const GROUPED = new Intl.NumberFormat("en-US")

export function formatLimit(value: number): string {
  return GROUPED.format(value)
}

/**
 * Whether a limit arrived larger than a JS number can hold exactly. The wire
 * carries these as BIGINT, and `JSON.parse` has already rounded anything past
 * `Number.MAX_SAFE_INTEGER` by the time it reaches here — so the value is
 * flagged rather than presented as though it were the number that was sold.
 */
export function beyondSafeInteger(value: number | null): boolean {
  return value !== null && !Number.isSafeInteger(value)
}

/**
 * A success-rate objective as a percentage. The wire value is a fraction in
 * (0, 1] and stays that way everywhere it is edited; this is display only, and
 * the rounding is here because 0.999 times 100 is not 99.9 in binary floating
 * point.
 */
export function formatObjective(value: number): string {
  return `${parseFloat((value * 100).toFixed(4))}%`
}

/** The objective window, which the plane bounds to 60..3600 seconds. */
export function formatWindow(seconds: number): string {
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600}h`
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60}m`
  return `${seconds}s`
}

export const COMMITTED_LIMITS = [
  "committed_rpm",
  "committed_tpm",
  "max_concurrent_requests",
  "max_output_tokens",
  "burst_rpm",
] as const

/**
 * Whether the row names no limit at all. Every axis uncommitted has the same
 * effect as having no row — the gateway limits nothing — and an operator
 * reading a table of commitments should be told that rather than left to
 * notice five blanks in a row.
 */
export function commitsNothing(quota: ServingQuota): boolean {
  return COMMITTED_LIMITS.every((name) => quota[name] === null)
}

const SHORTFALL_LABEL: Record<string, string> = {
  committed_rpm: "requests/min",
  committed_tpm: "tokens/min",
  success_rate_objective: "success rate",
}

/** Names the standard's own field in the words the table uses for it. */
export function shortfallLabel(field: string): string {
  return SHORTFALL_LABEL[field] ?? field.replace(/_/g, " ")
}
