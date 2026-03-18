import type { StatusTone } from "@/components/layout/status-dot"

const OPERATION_PHASE_TONES: Record<string, StatusTone> = {
  pending: "info",
  running: "warn",
  succeeded: "ok",
  failed: "danger",
  cancelled: "neutral",
  timed_out: "danger",
}

export function operationPhaseTone(phase: string): StatusTone {
  return OPERATION_PHASE_TONES[phase] ?? "neutral"
}

export const OPERATION_PHASES = [
  "pending",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]

const SEVERITY_TONES: Record<string, StatusTone> = {
  info: "info",
  warning: "warn",
  error: "danger",
  critical: "danger",
}

export function severityTone(severity: string): StatusTone {
  return SEVERITY_TONES[severity] ?? "neutral"
}

export const EVENT_SEVERITIES = ["info", "warning", "error", "critical"]

const ROLLOUT_TONES: Record<string, StatusTone> = {
  ready: "ok",
  rolling: "warn",
  failed: "danger",
  unknown: "neutral",
}

export function rolloutTone(phase: string): StatusTone {
  return ROLLOUT_TONES[phase] ?? "neutral"
}

const ALLOCATION_PHASE_TONES: Record<string, StatusTone> = {
  held: "ok",
  pending: "info",
  releasing: "warn",
  released: "neutral",
}

export function allocationPhaseTone(phase: string): StatusTone {
  return ALLOCATION_PHASE_TONES[phase] ?? "neutral"
}
