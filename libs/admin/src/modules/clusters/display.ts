import type { StatusTone } from "@/components/layout/status-dot"
import type { ClusterPhase, ComponentPhase, SyncState } from "./types"

export const CLUSTER_PHASE_TONE: Record<ClusterPhase, StatusTone> = {
  pending: "info",
  active: "ok",
  draining: "warn",
  disabled: "neutral",
}

export const SYNC_TONE: Record<SyncState, StatusTone> = {
  connected: "ok",
  reconnecting: "warn",
  disconnected: "danger",
}

export const SYNC_LABEL: Record<SyncState, string> = {
  connected: "Connected",
  reconnecting: "Reconnecting",
  disconnected: "Disconnected",
}

export const COMPONENT_PHASE_TONE: Record<ComponentPhase, StatusTone> = {
  healthy: "ok",
  upgrading: "info",
  degraded: "warn",
  missing: "danger",
}

export function humanize(value: string): string {
  return value.replace(/_/g, " ")
}
