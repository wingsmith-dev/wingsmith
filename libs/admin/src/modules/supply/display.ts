import type { StatusTone } from "@/components/layout/status-dot"
import type {
  DeviceHealth,
  NodeCondition,
  ProvisioningSource,
  TopologyKind,
} from "./types"

export const NODE_CONDITION_TONE: Record<NodeCondition, StatusTone> = {
  ready: "ok",
  not_ready: "danger",
  unknown: "neutral",
}

export const NODE_CONDITION_LABEL: Record<NodeCondition, string> = {
  ready: "Ready",
  not_ready: "Not ready",
  unknown: "Unknown",
}

export const DEVICE_HEALTH_TONE: Record<DeviceHealth, StatusTone> = {
  healthy: "ok",
  degraded: "warn",
  failed: "danger",
  unknown: "neutral",
}

export const TOPOLOGY_KIND_LABEL: Record<TopologyKind, string> = {
  nvswitch: "NVSwitch",
  nvlink: "NVLink",
  pcie: "PCIe root",
  numa: "NUMA node",
}

export const PROVISIONING_SOURCE_LABEL: Record<ProvisioningSource, string> = {
  owned_bare_metal: "Owned bare metal",
  colocation: "Colocation",
  cloud_reserved: "Cloud reserved",
  cloud_on_demand: "Cloud on demand",
}
