import type { StatusTone } from "@/components/layout/status-dot"

const NODE_CONDITION_TONES: Record<string, StatusTone> = {
  ready: "ok",
  not_ready: "danger",
  unknown: "neutral",
}

export function nodeConditionTone(condition: string): StatusTone {
  return NODE_CONDITION_TONES[condition] ?? "neutral"
}

const DEVICE_HEALTH_TONES: Record<string, StatusTone> = {
  healthy: "ok",
  degraded: "warn",
  unhealthy: "danger",
  unknown: "neutral",
}

export function deviceHealthTone(health: string): StatusTone {
  return DEVICE_HEALTH_TONES[health] ?? "neutral"
}

export const NODE_CONDITIONS = ["ready", "not_ready", "unknown"]

/** The health vocabulary the plane's own filters accept. */
export const DEVICE_HEALTHS = ["healthy", "degraded", "unhealthy", "unknown"]

const TOPOLOGY_KIND_LABELS: Record<string, string> = {
  nvswitch: "NVSwitch",
  nvlink: "NVLink",
  pcie: "PCIe root",
  numa: "NUMA node",
}

export function topologyKindLabel(kind: string): string {
  return TOPOLOGY_KIND_LABELS[kind] ?? kind
}

/** Devices report memory in MiB; nobody reads a GPU spec in mebibytes. */
export function gibibytes(mib: number | null): string {
  if (mib === null) return "—"
  return `${Math.round(mib / 1024)} GiB`
}

export function healthCount(
  byHealth: Record<string, number>,
  health: string
): number {
  return byHealth[health] ?? 0
}

/** Devices in a rollup that are anything other than healthy. */
export function unhealthyCount(rollup: {
  total: number
  by_health: Record<string, number>
}): number {
  return rollup.total - healthCount(rollup.by_health, "healthy")
}
