import type { Cluster } from "@/modules/clusters/types"
import type { Allocation } from "@/modules/machinery/types"
import type { Device, SupplyNode, TopologyDomain } from "@/modules/supply/types"
import type { Brick } from "./types"

/**
 * Board-shaped test data, built by hand.
 *
 * The offline fixtures are a fleet and are used for the smoke test; these are
 * for the unit tests, where a fabric with exactly two free devices in it has to
 * be exactly that and stay that way when somebody edits the demo data.
 */
const NOW = "2026-01-01T00:00:00Z"

export function cluster(name: string, region: string, id = name): Cluster {
  return {
    id,
    name,
    display_name: null,
    region,
    zone: null,
    residency: null,
    phase: "active",
    labels: {},
    capabilities: {},
    enrolled_at: NOW,
    created_at: NOW,
    updated_at: null,
    revision: "1",
    reachable: true,
    metrics_label: name,
    serving: null,
    sync: {
      state: "connected",
      last_sync_at: NOW,
      last_seen_at: NOW,
      operator_version: "1",
      lag_ms: 1,
      last_error: null,
      distribution: null,
      cloud: null,
      observed_region: region,
      read_only: false,
    },
  }
}

export function node(
  id: string,
  clusterId: string,
  options: { cordoned?: boolean } = {}
): SupplyNode {
  return {
    id,
    name: id,
    cluster: clusterId,
    cluster_name: clusterId,
    pool: null,
    pool_name: null,
    condition: "ready",
    conditions: [],
    cordoned: options.cordoned ?? false,
    ready: true,
    instance_type: null,
    cloud: null,
    zone: null,
    region: null,
    cpu_cores: null,
    memory_mib: null,
    cpu_arch: null,
    os_image: null,
    kernel_version: null,
    driver_version: null,
    operator_version: null,
    labels: {},
    taints: [],
    devices: { total: 0, by_health: {}, device_class: null, allocated: 0 },
    joined_at: NOW,
    observed_at: NOW,
    created_at: NOW,
    updated_at: null,
  }
}

export function domain(
  id: string,
  clusterId: string,
  nodeId: string,
  kind = "nvswitch"
): TopologyDomain {
  return {
    id,
    cluster: clusterId,
    cluster_name: clusterId,
    nodes: [nodeId],
    node_names: [nodeId],
    parent: null,
    kind,
    identifier: `${nodeId}/${kind}-0`,
    link_generation: null,
    link_bandwidth_gbps: 900,
    device_count: 0,
    spans_nodes: false,
    attributes: {},
    observed_at: NOW,
    created_at: NOW,
    updated_at: null,
  }
}

export function device(
  id: string,
  options: {
    cluster: string
    node: string
    index: number
    class?: string
    domain?: TopologyDomain
    health?: string
    allocatable?: boolean
    migParent?: string
    migProfile?: string
    migEnabled?: boolean
  }
): Device {
  return {
    id,
    cluster: options.cluster,
    cluster_name: options.cluster,
    node: options.node,
    node_name: options.node,
    device_uuid: `GPU-${id}`,
    device_index: options.index,
    device_class: options.class ?? "h100-80gb-sxm",
    vendor: "nvidia",
    model_name: null,
    pci_address: null,
    memory_mib: 81_920,
    compute_capability: "9.0",
    health: options.health ?? "healthy",
    health_reason: null,
    ecc_enabled: true,
    ecc_volatile_errors: 0,
    ecc_aggregate_errors: 0,
    ecc_pending_retirement: false,
    throttled: false,
    throttle_reasons: [],
    mig_enabled: options.migEnabled ?? false,
    mig_profile: options.migProfile ?? null,
    mig_parent_device_uuid: options.migParent ?? null,
    allocatable: options.allocatable ?? true,
    topology: options.domain
      ? {
          id: options.domain.id,
          kind: options.domain.kind,
          identifier: options.domain.identifier,
        }
      : null,
    observed_at: NOW,
    created_at: NOW,
    updated_at: null,
  }
}

export function allocation(
  id: string,
  options: {
    cluster: string
    workload: string
    devices: Device[]
    live?: boolean
    releasedDevices?: string[]
    count?: number
  }
): Allocation {
  return {
    id,
    cluster: options.cluster,
    cluster_name: options.cluster,
    project: "prj_1",
    project_slug: "core",
    workload: options.workload,
    replica: null,
    device_class: options.devices[0]?.device_class ?? "h100-80gb-sxm",
    device_count: options.count ?? options.devices.length,
    over_quota: false,
    phase: options.live === false ? "released" : "held",
    live: options.live ?? true,
    gang_complete: true,
    acquired_at: NOW,
    released_at: null,
    observed_at: NOW,
    created_at: NOW,
    updated_at: null,
    devices: options.devices.map((entry) => ({
      device: entry.id,
      device_uuid: entry.device_uuid,
      node: entry.node,
      live: !(options.releasedDevices ?? []).includes(entry.id),
      released_at: null,
    })),
  }
}

export function brick(options: Partial<Brick> = {}): Brick {
  return {
    key: "brick",
    label: "test-workload",
    detail: "",
    workload: "wkl_1",
    recipe: null,
    project: null,
    priority_class: null,
    project_slug: "core",
    device_class: "h100-80gb-sxm",
    device_count: 4,
    requires_single_topology_domain: true,
    topology_domain_kind: null,
    replicas: 1,
    strategy: "pack",
    cluster_floors: {},
    max_clusters: null,
    pinned: false,
    ...options,
  }
}
