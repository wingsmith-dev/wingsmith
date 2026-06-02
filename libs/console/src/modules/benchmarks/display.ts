import type { StatusTone } from "@/components/layout/status-dot"
import type {
  BenchmarkLevel,
  BenchmarkMode,
  BenchmarkTier,
  VerdictOutcome,
} from "./types"

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

/**
 * The three things a run can be a measurement of, and what each one claims.
 *
 * Spelled out wherever a level is shown, because "deployment" and "endpoint"
 * beside two numbers look like two rows of the same table when they are two
 * different measurements that must never be averaged into one.
 */
export const LEVEL_CLAIM: Record<BenchmarkLevel, string> = {
  deployment: "one cluster's configuration",
  workload: "the serving configuration, across every cluster it was placed on",
  endpoint: "what a caller gets through the public name",
}

export const BENCHMARK_LEVELS: BenchmarkLevel[] = [
  "deployment",
  "workload",
  "endpoint",
]

export const BENCHMARK_MODES: BenchmarkMode[] = [
  "single",
  "warmup",
  "ramp",
  "sweep",
  "soak",
]

/**
 * Which harness parameter each stepped mode walks. A mode absent from here is
 * a single run and takes no steps — the plane refuses steps on one, and this
 * console does not offer the field.
 */
export const STEPPED_MODES: Partial<Record<BenchmarkMode, string>> = {
  ramp: "concurrency",
  sweep: "input_tokens_mean",
}

export const BENCHMARK_TIERS: BenchmarkTier[] = ["L1", "L2"]

/**
 * The input-length bands, in the order the standard sets targets for them.
 *
 * The label strings are the contract and the boundaries are not repeated here:
 * they belong to a frozen criteria version, and a third copy in a console is a
 * third answer to what a two-year-old verdict meant. What each label reads as
 * is presentation, and that is all this map is.
 */
export const BAND_ORDER: string[] = [
  "lt6k",
  "6k_16k",
  "16k_32k",
  "32k_64k",
  "64k_128k",
  "128k_256k",
  "gt256k",
  "unknown",
]

const BAND_LABELS: Record<string, string> = {
  lt6k: "< 6K",
  "6k_16k": "6K – 16K",
  "16k_32k": "16K – 32K",
  "32k_64k": "32K – 64K",
  "64k_128k": "64K – 128K",
  "128k_256k": "128K – 256K",
  gt256k: "> 256K",
  unknown: "undeclared",
}

/** A band label as prose. An unrecognised label is shown as itself, not dropped. */
export function bandLabel(band: string): string {
  return BAND_LABELS[band] ?? band
}

/** Bands in standard order, with anything unrecognised kept at the end. */
export function orderBands<T extends { band: string }>(bands: T[]): T[] {
  return [...bands].sort((a, b) => {
    const left = BAND_ORDER.indexOf(a.band)
    const right = BAND_ORDER.indexOf(b.band)
    return (
      (left === -1 ? BAND_ORDER.length : left) -
      (right === -1 ? BAND_ORDER.length : right)
    )
  })
}

const VERDICT_TONES: Record<VerdictOutcome, StatusTone> = {
  pass: "ok",
  fail: "danger",
  inconclusive: "warn",
  not_applicable: "neutral",
}

/**
 * `inconclusive` is warned rather than greyed. A missing measurement is not a
 * pass and is not nothing — it is the outcome somebody has to go and fix.
 */
export function verdictTone(outcome: VerdictOutcome): StatusTone {
  return VERDICT_TONES[outcome] ?? "neutral"
}

const VERDICT_LABELS: Record<VerdictOutcome, string> = {
  pass: "pass",
  fail: "fail",
  inconclusive: "inconclusive",
  not_applicable: "not applicable",
}

export function verdictLabel(outcome: VerdictOutcome): string {
  return VERDICT_LABELS[outcome] ?? outcome
}

/** Milliseconds as the standard quotes them, seconds once past a second. */
export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—"
  if (value >= 1000)
    return `${(value / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 })} s`
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} ms`
}
