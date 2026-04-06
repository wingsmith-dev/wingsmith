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
