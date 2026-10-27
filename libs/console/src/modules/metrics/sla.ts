import type { SlaReport } from "./types"

/** One month the SLA card can show, as the plane names it and as a person reads it. */
export interface SlaMonth {
  /** `YYYY-MM`, UTC. */
  month: string
  label: string
  /** The month still running, measured up to now. */
  running: boolean
}

/**
 * The current calendar month and the ones before it, newest first.
 *
 * UTC, because that is what the plane reads `month` in: a month picked in a
 * browser west of Greenwich on the evening of the last day must not quietly
 * become the next one.
 */
export function slaMonths(now: Date, count = 3): SlaMonth[] {
  const months: SlaMonth[] = []
  for (let back = 0; back < count; back++) {
    const at = new Date(
      Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - back, 1)
    )
    const month = `${at.getUTCFullYear()}-${String(at.getUTCMonth() + 1).padStart(2, "0")}`
    const label = at.toLocaleString("en-US", {
      month: "short",
      year: back === 0 || at.getUTCMonth() === 11 ? "numeric" : undefined,
      timeZone: "UTC",
    })
    months.push({ month, label, running: back === 0 })
  }
  return months
}

/**
 * A percentage to the precision an SLA is argued at.
 *
 * Three places, because at three nines the console's usual whole-percent
 * rounding turns 99.94% and 99.86% (one met, one missed) into the same 100%.
 */
export function slaPercent(value: number | null, digits = 3): string {
  if (value === null || Number.isNaN(value)) return "—"
  return `${value.toFixed(digits)}%`
}

/** The objective as a percentage, from the fraction the quota stores. */
export function objectivePercent(report: SlaReport): number | null {
  const rate = report.objective?.success_rate
  return rate == null ? null : rate * 100
}

export type SlaTone = "default" | "ok" | "warn" | "danger"

/**
 * The verdict as a word first and a color second, so it reads without color.
 *
 * "No objective" and "Not measured" are different answers: one is a commitment
 * nobody declared, the other a period nothing measured, and neither is a pass.
 */
export function slaVerdict(report: SlaReport): {
  label: string
  tone: SlaTone
} {
  if (!report.objective || report.objective.success_rate === null) {
    return { label: "No objective declared", tone: "default" }
  }
  if (report.met === null) return { label: "Not measured", tone: "default" }
  if (report.met) {
    return {
      label: report.partial ? "On track" : "Met",
      tone: "ok",
    }
  }
  return report.partial
    ? { label: "Over budget so far", tone: "warn" }
    : { label: "Missed", tone: "danger" }
}

/**
 * How much of the error budget is gone, as words and as a bar width.
 *
 * The bar stops at the end of its track; the words do not, because 340% of a
 * budget is a different conversation from 101%.
 */
export function budgetReading(consumed: number | null): {
  label: string
  width: number
  tone: SlaTone
} {
  if (consumed === null || Number.isNaN(consumed)) {
    return { label: "—", width: 0, tone: "default" }
  }
  const percent = consumed * 100
  return {
    label: `${percent < 10 ? percent.toFixed(1) : Math.round(percent)}% used`,
    width: Math.min(100, Math.max(0, percent)),
    tone: consumed > 1 ? "danger" : consumed > 0.75 ? "warn" : "ok",
  }
}

/** A request count a person can read at a glance: 9.9M, 412K, 87. */
export function compactCount(value: number | null): string {
  if (value === null) return "—"
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 10_000) return `${Math.round(value / 1_000)}K`
  return value.toLocaleString("en-US")
}
