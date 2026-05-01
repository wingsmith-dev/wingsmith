/**
 * The arithmetic the performance views do over what the plane sent.
 *
 * The plane merges what merges and refuses the rest, so everything here obeys
 * the same rule: rates and counts add across clusters because a rate over two
 * clusters is a rate over their union, and nothing else does. A utilization is
 * kept per cluster and drawn twice rather than averaged into a figure true of
 * no card. An hour no store reported is absent, never zero — an empty bucket
 * and an idle one are different facts and only one of them is a measurement.
 */

import type {
  MetricSample,
  MetricSeries,
  MetricSeriesPoint,
  Metrics,
} from "./types"

/**
 * One metric's curves added across clusters, aligned by timestamp.
 *
 * Only for the metrics the catalogue calls `aggregate`. A bucket no cluster
 * reported is dropped rather than counted as zero.
 */
export function sumCurvePoints(
  series: MetricSeries[],
  metric: string
): MetricSeriesPoint[] {
  const totals = new Map<number, number>()
  for (const curve of series) {
    if (curve.metric !== metric) continue
    for (const point of curve.points) {
      totals.set(point.at, (totals.get(point.at) ?? 0) + point.value)
    }
  }
  return [...totals.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([at, value]) => ({ at, value }))
}

/** The same sum, for a chart that carries its own axis and wants values only. */
export function sumCurves(series: MetricSeries[], metric: string): number[] {
  return sumCurvePoints(series, metric).map((point) => point.value)
}

export interface UtilizationWeek {
  label: string
  /** `[day][hour]`, Monday first. `null` is an hour nothing reported. */
  grid: (number | null)[][]
}

/**
 * The week's GPU utilization as a day-by-hour grid, one grid per curve.
 *
 * Not averaged across clusters, for the reason the plane will not average them
 * either: eight cards at 90% in one cluster and one at 10% in another is not a
 * fleet at 50%. Two clusters draw two grids.
 */
export function utilizationWeek(data: Metrics | undefined): UtilizationWeek[] {
  return (data?.series ?? [])
    .filter(
      (curve) => curve.metric === "gpu.utilization" && curve.points.length > 0
    )
    .map((curve) => ({
      label: curve.labels.cluster ?? curve.labels.instance ?? "utilization",
      grid: hourGrid(curve),
    }))
}

function hourGrid(curve: MetricSeries): (number | null)[][] {
  const sums = Array.from({ length: 7 }, () => new Array<number>(24).fill(0))
  const counts = Array.from({ length: 7 }, () => new Array<number>(24).fill(0))
  for (const point of curve.points) {
    const at = new Date(point.at * 1000)
    /** Monday first, so the two weekend rows sit together at the bottom. */
    const day = (at.getDay() + 6) % 7
    const hour = at.getHours()
    sums[day][hour] += point.value
    counts[day][hour] += 1
  }
  return sums.map((row, day) =>
    row.map((total, hour) =>
      counts[day][hour] ? total / counts[day][hour] : null
    )
  )
}

/** The mean of every utilization point in the answer, or nothing measured. */
export function meanUtilization(data: Metrics | undefined): number | null {
  const points = (data?.series ?? [])
    .filter((curve) => curve.metric === "gpu.utilization")
    .flatMap((curve) => curve.points)
  if (points.length === 0) return null
  return points.reduce((sum, point) => sum + point.value, 0) / points.length
}

/**
 * Cards held × hours, less the share of that time they were busy.
 *
 * A derivation and not a measurement, and it holds only while the workload kept
 * the same cards for the whole span — so it answers nothing when placement
 * reports no cards or the store reported no hours.
 */
export function idleGpuHours(
  data: Metrics | undefined,
  gpuCount: number,
  spanSeconds: number
): number | null {
  const mean = meanUtilization(data)
  if (mean === null || gpuCount === 0) return null
  return (spanSeconds / 3600) * gpuCount * (1 - mean / 100)
}

export interface AvailabilityDay {
  /** Local midnight of the day, in epoch seconds. */
  at: number
  /** The day's mean availability, or nothing for a day no store reported. */
  value: number | null
}

/**
 * An endpoint's availability as one figure per calendar day, oldest first.
 *
 * The row always has `days` cells, because the shape of the strip is the point:
 * a fortnight drawn as four cells reads as a quiet fortnight rather than as ten
 * days nobody measured. A day the store answered nothing is `null` and must be
 * drawn as absence — a front door nobody called was not 0% available.
 *
 * Local midnight, not UTC, because the reader compares these cells against a
 * day they remember.
 */
export function availabilityDays(
  data: Metrics | undefined,
  days: number
): AvailabilityDay[] {
  const sums = new Map<number, { total: number; count: number }>()
  for (const curve of data?.series ?? []) {
    if (curve.metric !== "serving.availability") continue
    for (const point of curve.points) {
      const key = startOfDay(point.at)
      const bucket = sums.get(key) ?? { total: 0, count: 0 }
      bucket.total += point.value
      bucket.count += 1
      sums.set(key, bucket)
    }
  }
  const today = startOfDay(Date.now() / 1000)
  return Array.from({ length: days }, (_, i) => {
    const at = startOfDay(today - (days - 1 - i) * 86400)
    const bucket = sums.get(at)
    return { at, value: bucket ? bucket.total / bucket.count : null }
  })
}

/** Local midnight of the day an epoch-seconds instant falls in. */
function startOfDay(seconds: number): number {
  const at = new Date(seconds * 1000)
  at.setHours(0, 0, 0, 0)
  return Math.floor(at.getTime() / 1000)
}

export interface LatencyStage {
  metric: string
  stage: string
}

/**
 * The stages of a request in the order it passes through them.
 *
 * Every one is a p95 except where the catalogue registers its own percentile,
 * and the label carries which — a column of percentiles that were never asked
 * for would be four numbers the store does not hold.
 */
export const LATENCY_STAGES: readonly LatencyStage[] = [
  { metric: "inference.queue_time", stage: "Waiting in queue" },
  { metric: "inference.ttft_p50", stage: "Time to first token (p50)" },
  { metric: "inference.ttft_p90", stage: "Time to first token (p90)" },
  { metric: "inference.ttft", stage: "Time to first token (p95)" },
  { metric: "inference.prefill_time", stage: "Prefill" },
  { metric: "inference.decode_time", stage: "Decode" },
  { metric: "inference.tpot", stage: "Per output token" },
  { metric: "inference.latency", stage: "End to end" },
]

/** The stages the store answered, in reading order. The rest do not get a row. */
export function latencyStages(samples: MetricSample[]): LatencyStage[] {
  const present = new Set(samples.map((sample) => sample.metric))
  return LATENCY_STAGES.filter((stage) => present.has(stage.metric))
}
