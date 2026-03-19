import type { MetricSample, MetricUnit } from "./types"

const SUFFIX: Record<string, string> = {
  percent: "%",
  celsius: "°C",
  watts: " W",
  mhz: " MHz",
  per_second: "/s",
}

/** Renders a measurement, and renders absence as absence. */
export function formatMetric(value: number | null, unit: MetricUnit): string {
  if (value === null || Number.isNaN(value)) return "—"
  if (unit === "mib") {
    return value >= 1024
      ? `${(value / 1024).toFixed(1)} GiB`
      : `${Math.round(value)} MiB`
  }
  if (unit === "seconds") {
    return value < 1 ? `${Math.round(value * 1000)} ms` : `${value.toFixed(2)} s`
  }
  if (unit === "count") return Math.round(value).toLocaleString()
  const digits = unit === "percent" || unit === "celsius" ? 0 : 1
  return `${value.toFixed(digits)}${SUFFIX[unit] ?? ""}`
}

export function sampleOf(samples: MetricSample[], metric: string): MetricSample | undefined {
  return samples.find((s) => s.metric === metric)
}

export function valueOf(samples: MetricSample[], metric: string): number | null {
  return sampleOf(samples, metric)?.value ?? null
}

/**
 * Thresholds for the two GPU readings that mean "go look at the hardware".
 * Deliberately conservative: 84°C is where an SXM card starts trimming clocks
 * and 90°C is its slowdown point, so warn well below the first.
 */
export function temperatureTone(celsius: number | null): "ok" | "warn" | "danger" {
  if (celsius === null) return "ok"
  if (celsius >= 87) return "danger"
  if (celsius >= 80) return "warn"
  return "ok"
}

export function utilizationTone(percent: number | null): "ok" | "warn" | "danger" {
  if (percent === null) return "ok"
  if (percent < 5) return "warn"
  return "ok"
}
