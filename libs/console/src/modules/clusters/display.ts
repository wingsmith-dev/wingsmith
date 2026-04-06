import type { StatusTone } from "@/components/layout/status-dot"
import type { ClusterPhase, SyncState } from "./types"

export const CLUSTER_PHASE_TONE: Record<ClusterPhase, StatusTone> = {
  pending: "info",
  active: "ok",
  draining: "warn",
  disabled: "neutral",
}

export const CLUSTER_PHASES: ClusterPhase[] = [
  "pending",
  "active",
  "draining",
  "disabled",
]

const SYNC_TONES: Record<string, StatusTone> = {
  connected: "ok",
  disconnected: "danger",
  never_connected: "neutral",
}

/**
 * `sync.state` is derived by the plane, so an unknown value is rendered
 * neutrally rather than crashing a lookup that assumed a closed set.
 */
export function syncTone(state: SyncState): StatusTone {
  return SYNC_TONES[state] ?? "warn"
}

export function humanize(value: string): string {
  return value.replace(/_/g, " ")
}

export function clusterLabel(cluster: {
  display_name: string | null
  name: string
}) {
  return cluster.display_name ?? cluster.name
}
