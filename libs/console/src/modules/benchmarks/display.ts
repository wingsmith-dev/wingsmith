import type { StatusTone } from "@/components/layout/status-dot"

const PHASE_TONES: Record<string, StatusTone> = {
  pending: "info",
  running: "warn",
  succeeded: "ok",
  failed: "danger",
  cancelled: "neutral",
  timed_out: "danger",
}

export function benchmarkPhaseTone(phase: string): StatusTone {
  return PHASE_TONES[phase] ?? "neutral"
}

export const BENCHMARK_PHASES = [
  "pending",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]

/**
 * A statistic block as harnesses tend to emit one, if that is what this is.
 *
 * Deliberately a guess and named like one. The plane stores the harness's shape
 * verbatim, so this console recognises the common `{unit, avg, p50, …}` object
 * to render it as a row of percentiles and falls back to raw JSON when it does
 * not — which is the honest behaviour for a field nothing promised the shape of.
 */
export interface StatBlock {
  unit?: string
  avg?: number
  min?: number
  max?: number
  p50?: number
  p90?: number
  p95?: number
  p99?: number
  std?: number
}

const STAT_KEYS = ["avg", "min", "max", "p50", "p90", "p95", "p99", "std"]

export function asStatBlock(value: unknown): StatBlock | null {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return null
  const record = value as Record<string, unknown>
  const numeric = STAT_KEYS.filter((key) => typeof record[key] === "number")
  if (numeric.length === 0) return null
  const unknownKeys = Object.keys(record).filter(
    (key) => key !== "unit" && !STAT_KEYS.includes(key)
  )
  if (unknownKeys.length > 0) return null
  return record as StatBlock
}

/** A metric name as a harness writes it, made readable without losing it. */
export function metricLabel(key: string): string {
  return key.replace(/_/g, " ")
}

export function formatStat(value: number | undefined): string {
  if (value === undefined) return "—"
  if (Number.isInteger(value)) return value.toLocaleString()
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

/**
 * The knobs worth showing beside a run in a list, in the order they matter.
 *
 * Not every parameter: a row that showed all ten would be unreadable, and the
 * three that change what was measured most are the load, the prompt and the
 * generation length.
 */
export function loadSummary(parameters: Record<string, unknown>): string {
  const parts: string[] = []
  if (parameters.concurrency !== undefined)
    parts.push(`c=${parameters.concurrency}`)
  if (parameters.request_count !== undefined)
    parts.push(`n=${parameters.request_count}`)
  if (parameters.input_tokens_mean !== undefined)
    parts.push(`in=${parameters.input_tokens_mean}`)
  if (parameters.output_tokens_mean !== undefined)
    parts.push(`out=${parameters.output_tokens_mean}`)
  return parts.join(" ")
}
