import type { StatusTone } from "@/components/layout/status-dot"
import { formatMetric } from "@/modules/metrics/format"

import type { AlertThreshold, MetricRule } from "./types"

const STATE_TONES: Record<string, StatusTone> = {
  pending: "info",
  firing: "danger",
  acknowledged: "warn",
  resolved: "neutral",
}

export function alertStateTone(state: string): StatusTone {
  return STATE_TONES[state] ?? "neutral"
}

export const ALERT_STATES = ["pending", "firing", "acknowledged", "resolved"]

export const ALERT_SEVERITIES = ["info", "warning", "error", "critical"]

/** Still someone's problem. A resolved alert is history and stops polling. */
export function isOpenAlert(state: string): boolean {
  return state !== "resolved"
}

export function comparisonWord(comparison: string): string {
  if (comparison === "above") return "above"
  if (comparison === "below") return "below"
  return comparison
}

/**
 * The declared number in the unit the metric is measured in, so the commitment
 * and the measurement read the same way.
 */
export function formatThresholdValue(
  value: number,
  unit: string | undefined
): string {
  return formatMetric(value, unit ?? "")
}

export function formatSeconds(seconds: number): string {
  if (seconds === 0) return "immediately"
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  if (seconds % 60 === 0) return `${seconds / 60}m`
  return `${seconds}s`
}

/**
 * A declared window, or the rule's own if this row left it alone.
 *
 * Null on a threshold means "whatever the rule says", so the fallback is the
 * catalogue's and never a constant here — and callers are told which of the two
 * they got, because a value shown without its provenance reads as pinned.
 */
export interface Inherited<T> {
  value: T
  /** True when the value came from the rule rather than from the row. */
  fromRule: boolean
}

export function effectiveLookback(
  threshold: AlertThreshold,
  rule: MetricRule | undefined
): Inherited<string | null> {
  if (threshold.lookback !== null)
    return { value: threshold.lookback, fromRule: false }
  return { value: rule?.lookback ?? null, fromRule: true }
}

export function effectiveForSeconds(
  threshold: AlertThreshold,
  rule: MetricRule | undefined
): Inherited<number | null> {
  if (threshold.for_seconds !== null)
    return { value: threshold.for_seconds, fromRule: false }
  return { value: rule?.for_seconds ?? null, fromRule: true }
}

export function effectiveSeverity(
  threshold: AlertThreshold,
  rule: MetricRule | undefined
): Inherited<string | null> {
  if (threshold.severity !== null)
    return { value: threshold.severity, fromRule: false }
  return { value: rule?.severity ?? null, fromRule: true }
}

/** A stable rendering of the narrowing labels, matching the plane's dedupe suffix. */
export function dimensionKey(dimensions: Record<string, string>): string {
  const keys = Object.keys(dimensions).sort()
  return keys.map((key) => `${key}=${dimensions[key]}`).join(",")
}

/**
 * Most specific first: a row about this subject, then the fewest narrowing
 * dimensions, then the key itself so the order is total. Mirrors
 * `_specificity` in `app/reactor/rules.py` — a console that ranked these
 * differently would show one commitment and the reactor would use another.
 */
function specificity(threshold: AlertThreshold): [number, number, string] {
  return [
    threshold.subject.id !== null ? 0 : 1,
    Object.keys(threshold.dimensions).length,
    dimensionKey(threshold.dimensions),
  ]
}

function compareSpecificity(a: AlertThreshold, b: AlertThreshold): number {
  const left = specificity(a)
  const right = specificity(b)
  if (left[0] !== right[0]) return left[0] - right[0]
  if (left[1] !== right[1]) return left[1] - right[1]
  return left[2] < right[2] ? -1 : left[2] > right[2] ? 1 : 0
}

/**
 * Which threshold actually applies to one subject, and what it beat.
 *
 * `ignored` is the reason this exists on a screen. A disabled override is not a
 * candidate — the reactor only loads enabled rows — so the fleet default is
 * what fires, and an operator who cannot see that will raise the number on the
 * row that is switched off.
 */
export interface ThresholdResolution {
  applied: AlertThreshold | null
  /** True when what applies is the fleet default rather than a row about this subject. */
  inherited: boolean
  /** Enabled rows that lost, most specific first. */
  shadowed: AlertThreshold[]
  /** Rows that would have been candidates if they were enabled. */
  ignored: AlertThreshold[]
}

export function resolveThreshold(
  thresholds: AlertThreshold[],
  rule: string,
  subjectUid: string | null
): ThresholdResolution {
  const scoped = thresholds.filter(
    (threshold) =>
      threshold.rule === rule &&
      (threshold.subject.id === null || threshold.subject.id === subjectUid)
  )
  const candidates = scoped
    .filter((threshold) => threshold.enabled)
    .sort(compareSpecificity)
  const applied = candidates[0] ?? null
  return {
    applied,
    inherited: applied !== null && applied.subject.id === null,
    shadowed: candidates.slice(1),
    ignored: scoped.filter((threshold) => !threshold.enabled),
  }
}

/** The fleet default for a rule, enabled or not — there is at most one. */
export function fleetDefaultFor(
  thresholds: AlertThreshold[],
  rule: string
): AlertThreshold | undefined {
  return thresholds.find(
    (threshold) => threshold.rule === rule && threshold.subject.id === null
  )
}

/** Every row that narrows a rule to one subject, fleet default excluded. */
export function overridesFor(
  thresholds: AlertThreshold[],
  rule: string
): AlertThreshold[] {
  return thresholds.filter(
    (threshold) => threshold.rule === rule && threshold.subject.id !== null
  )
}

/**
 * What one row is, said in the table it sits in: whether it is the rule's fleet
 * default and how many subjects have taken themselves out of it, or whether it
 * is an override and what it overrides.
 */
export function precedenceNote(
  threshold: AlertThreshold,
  thresholds: AlertThreshold[]
): string {
  if (threshold.subject.id === null) {
    const overridden = overridesFor(thresholds, threshold.rule).filter(
      (other) => other.enabled
    ).length
    if (overridden === 0) return "applies to every subject"
    return `applies except to ${overridden} overridden ${overridden === 1 ? "subject" : "subjects"}`
  }
  if (!threshold.enabled) return "disabled — the fleet default applies"
  const fleet = fleetDefaultFor(thresholds, threshold.rule)
  if (!fleet) return "the only commitment for this rule"
  if (!fleet.enabled) return "overrides a disabled fleet default"
  return "overrides the fleet default"
}
