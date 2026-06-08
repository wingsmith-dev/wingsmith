import type { StatusTone } from "@/components/layout/status-dot"
import {
  curveProgress,
  isOpen,
  reading,
  type BenchmarkCurve,
  type BenchmarkCurvePoint,
  type BenchmarkLevel,
  type BenchmarkMode,
  type BenchmarkTier,
  type VerdictOutcome,
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

/* ---- load curves ------------------------------------------------------ */

/**
 * How each stepped parameter reads on an axis label.
 *
 * The plane names the field a request walked and this only spells it, so a
 * field nobody here has heard of is shown as itself with its underscores taken
 * out rather than dropped or guessed at.
 */
const STEP_FIELD_LABELS: Record<string, string> = {
  concurrency: "Concurrency",
  input_tokens_mean: "Input tokens",
  output_tokens_mean: "Output tokens",
}

export function stepFieldLabel(field: string): string {
  return STEP_FIELD_LABELS[field] ?? metricLabel(field)
}

/** Which of the two axes a curve reading belongs on. They do not share a scale. */
export type CurveAxis = "latency" | "throughput"

export type CurveTone = "primary" | "warn" | "danger" | "muted"

/**
 * One number a curve may draw, named as the plane names it.
 *
 * `display` marks a reading no verdict may ever reach. The aggregate output
 * rate is the whole reason the mark exists: it clears a per-stream floor under
 * every possible condition, so a check that could read it is a check that can
 * never fail. It is drawn because it is the shape of the trade-off, and it is
 * labelled so that nobody mistakes a picture for an assertion.
 */
export interface CurveMetric {
  id: string
  label: string
  axis: CurveAxis
  tone: CurveTone
  display?: boolean
}

/** The readings drawn as lines, latency against throughput, in legend order. */
export const CURVE_LINES: CurveMetric[] = [
  { id: "ttft_p50_ms", label: "TTFT p50", axis: "latency", tone: "primary" },
  { id: "ttft_p95_ms", label: "TTFT p95", axis: "latency", tone: "danger" },
  {
    id: "output_tps_per_stream",
    label: "Output per stream",
    axis: "throughput",
    tone: "primary",
  },
  {
    id: "output_tps_total",
    label: "Output aggregate",
    axis: "throughput",
    tone: "muted",
    display: true,
  },
]

/** The readings shown as text beside a step, in the order they answer for it. */
export const CURVE_FIELDS: CurveMetric[] = [
  ...CURVE_LINES,
  { id: "ttft_p99_ms", label: "TTFT p99", axis: "latency", tone: "muted" },
  { id: "itl_p50_ms", label: "ITL p50", axis: "latency", tone: "muted" },
  { id: "e2e_p95_ms", label: "End to end p95", axis: "latency", tone: "muted" },
  {
    id: "request_throughput",
    label: "Requests",
    axis: "throughput",
    tone: "muted",
    display: true,
  },
]

/** A reading with its unit as the plane quoted it, or an em dash for a miss. */
export function formatReading(
  value: number | null,
  unit: string | null
): string {
  if (value === null) return "—"
  if (unit === "ms") return formatMs(value)
  const number = value.toLocaleString(undefined, { maximumFractionDigits: 2 })
  return unit ? `${number} ${unit}` : number
}

/**
 * One reading of every step, in axis order, ready to be drawn.
 *
 * The phase and the discard flag travel with the value because the chart has to
 * tell three absences apart: a step that has not run yet, a warm-up that is not
 * part of the measurement, and a run that finished and reported nothing.
 */
export interface CurveSample {
  index: number
  step: number | null
  value: number | null
  phase: string
  discard: boolean
  run: string
}

export function curveSamples(
  points: BenchmarkCurvePoint[],
  metric: string
): CurveSample[] {
  return [...points]
    .sort((a, b) => a.step_index - b.step_index)
    .map((point, index) => ({
      index,
      step: point.step_value,
      value: reading(point, metric),
      phase: point.phase,
      discard: point.discard,
      run: point.run,
    }))
}

/**
 * The stretches of a series a line may actually be drawn through.
 *
 * A null reading ends a segment. It is the one rule this whole chart exists
 * under: a named miss is a hole in the curve, and interpolating across it — or
 * worse, plotting it at zero — would draw a measurement nobody took.
 *
 * A discarded step is excluded rather than treated as a hole: a warm-up is
 * counted by nothing, so the line joins the counted steps either side of it and
 * the warm-up is drawn as its own mark instead.
 */
export function curveSegments(samples: CurveSample[]): CurveSample[][] {
  const segments: CurveSample[][] = []
  let current: CurveSample[] = []
  for (const sample of samples) {
    if (sample.discard) continue
    if (sample.value === null) {
      if (current.length) segments.push(current)
      current = []
      continue
    }
    current.push(sample)
  }
  if (current.length) segments.push(current)
  return segments
}

/**
 * The ceiling one axis is drawn against, over every series sharing it.
 *
 * Anchored at zero and headroomed by a tenth, because both a latency and a
 * throughput axis mean something at zero and a curve read against a floating
 * baseline exaggerates every wobble. Null when nothing on the axis measured.
 */
export function curveCeiling(series: CurveSample[][]): number | null {
  const values = series
    .flat()
    .filter((sample) => !sample.discard && sample.value !== null)
    .map((sample) => sample.value as number)
  if (values.length === 0) return null
  return Math.max(...values) * 1.1 || 1
}

/** Where a step sits across the plot. One step is centred rather than at the edge. */
export function curveX(index: number, count: number, width: number): number {
  if (count <= 1) return width / 2
  return (index / (count - 1)) * width
}

/** Where a value sits up the plot, against a zero-anchored ceiling. */
export function curveY(value: number, ceiling: number, height: number): number {
  return height - (value / (ceiling || 1)) * height
}

/** An SVG polyline point list for one unbroken stretch of a series. */
export function curvePath(
  segment: CurveSample[],
  count: number,
  width: number,
  ceiling: number,
  height: number
): string {
  return segment
    .map(
      (sample) =>
        `${curveX(sample.index, count, width).toFixed(1)},${curveY(
          sample.value as number,
          ceiling,
          height
        ).toFixed(1)}`
    )
    .join(" ")
}

/**
 * What a curve is showing at all, before anything is drawn.
 *
 * Five different answers, and the reason they are one function is that four of
 * them are not the chart: a ramp nobody started, a ramp with nothing back yet,
 * a ramp every step of which failed and a single point are each a sentence
 * somebody has to read, not an empty axis.
 */
export type CurveState = "none" | "empty" | "all-failed" | "single" | "curve"

export function curveState(curve: BenchmarkCurve | undefined): CurveState {
  if (!curve) return "none"
  const counted = curve.points.filter((point) => !point.discard)
  if (counted.length === 0) return "empty"
  const reported = counted.filter((point) => point.phase === "succeeded")
  const open = counted.filter((point) => isOpen(point.phase))
  if (reported.length === 0 && open.length === 0) return "all-failed"
  if (counted.length === 1) return "single"
  return "curve"
}

/**
 * Where in the drawn order the knee sits, or -1.
 *
 * The plane names a `step_index` and not a position, and the two are different
 * whenever a request walked its steps out of order — a ramp asking for
 * `[16, 4, 64]` puts index 0 in the middle of the axis. Drawn against the
 * position and the amber rule would land on the wrong step.
 */
export function kneeIndex(curve: BenchmarkCurve | undefined): number {
  if (!curve || !curve.knee.found) return -1
  return [...curve.points]
    .sort((a, b) => a.step_index - b.step_index)
    .findIndex((point) => point.step_index === curve.knee.step_index)
}

/**
 * The knee as prose, whichever of the four things it is.
 *
 * A found knee whose step value never resolved is the fourth: the plane says
 * which step bent and cannot say at what load, which is a named miss and not a
 * step at zero — so it is spelled, never rendered as the word `null`.
 */
export function kneeLabel(curve: BenchmarkCurve): string {
  if (!curve.knee.found) return "not found"
  if (curve.knee.step_value === null)
    return `step ${curve.knee.step_index ?? "—"}, load unresolved`
  return `${curve.step_field} ${curve.knee.step_value}`
}

/**
 * The knee as a stat tile reads it.
 *
 * `small` is the tile's own signal that the value is prose rather than a
 * number, and an absent knee is prose every time: "no knee" and "not enough
 * steps to say" are different facts and the tile must not flatten them into a
 * dash.
 */
export function kneeTile(curve: BenchmarkCurve | undefined): {
  value: string
  small: boolean
} {
  if (!curve) return { value: "—", small: false }
  if (!curve.knee.found) {
    const progress = curveProgress(curve)
    return {
      value: progress.open > 0 ? "still ramping" : "not found",
      small: true,
    }
  }
  if (curve.knee.step_value === null)
    return { value: "load unresolved", small: true }
  return { value: String(curve.knee.step_value), small: false }
}
