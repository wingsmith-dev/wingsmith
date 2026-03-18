import type { Cluster } from "@/modules/clusters/types"
import type {
  Artifact,
  Deployment,
  Endpoint,
  Recipe,
} from "@/modules/demand/types"
import type {
  Policy,
  PriorityClass,
  Project,
  Quota,
} from "@/modules/binding/types"
import type {
  Allocation,
  Component,
  Operation,
  PlaneEvent,
} from "@/modules/machinery/types"
import type {
  Device,
  Pool,
  SupplyNode,
  TopologyDomain,
} from "@/modules/supply/types"
import { revision, uid, uuid } from "./ids"

/**
 * The offline corpus. Every row is typed as the schema the plane actually
 * returns, so a field the server does not send cannot be invented here and a
 * field it does send cannot be forgotten — a fixture that disagrees with the
 * server is worse than no fixture, because it makes a broken client's tests
 * pass.
 */

const NOW = Date.parse("2026-08-19T18:00:00.000Z")

const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString()

const CREATED = iso(60)
const REV = revision(CREATED)

interface NodeSpec {
  suffix: string
  condition: string
  cordoned: boolean
  devices: number
  unhealthy: number
}

interface PoolSpec {
  name: string
  deviceClass: string
  devicesPerNode: number
  minNodes: number
  maxNodes: number
  sku: string
  topologyKind: string
  bandwidth: number | null
  nodes: NodeSpec[]
}

interface ClusterSpec {
  name: string
  displayName: string | null
  region: string
  zone: string | null
  residency: string | null
  phase: Cluster["phase"]
  reachable: boolean
  labels: Record<string, string>
  capabilities: Record<string, unknown>
  syncState: string
  agentVersion: string | null
  lastError: string | null
  enrolled: boolean
  pools: PoolSpec[]
}

const SPECS: ClusterSpec[] = [
  {
    name: "iad-1",
    displayName: "Ashburn 1",
    region: "us-east-1",
    zone: "us-east-1b",
    residency: "us",
    phase: "active",
    reachable: true,
    labels: { tier: "prod", fabric: "nvswitch" },
    capabilities: { nvlink: true, rdma: true, mig: true },
    syncState: "connected",
    agentVersion: "0.4.2",
    lastError: null,
    enrolled: true,
    pools: [
      {
        name: "h100-prod",
        deviceClass: "h100-80",
        devicesPerNode: 8,
        minNodes: 4,
        maxNodes: 16,
        sku: "8xh100-80",
        topologyKind: "nvswitch",
        bandwidth: 900,
        nodes: [
          { suffix: "n01", condition: "ready", cordoned: false, devices: 8, unhealthy: 1 },
          { suffix: "n02", condition: "ready", cordoned: false, devices: 8, unhealthy: 0 },
          { suffix: "n03", condition: "ready", cordoned: true, devices: 8, unhealthy: 0 },
          { suffix: "n04", condition: "not_ready", cordoned: true, devices: 8, unhealthy: 8 },
          { suffix: "n05", condition: "ready", cordoned: false, devices: 8, unhealthy: 0 },
        ],
      },
    ],
  },
  {
    name: "sjc-1",
    displayName: "San Jose 1",
    region: "us-west-2",
    zone: null,
    residency: "us",
    phase: "active",
    reachable: false,
    labels: { tier: "prod" },
    capabilities: { nvlink: true },
    syncState: "disconnected",
    agentVersion: "0.4.1",
    lastError: "stream closed by peer",
    enrolled: true,
    pools: [
      {
        name: "l40s-serving",
        deviceClass: "l40s-48",
        devicesPerNode: 4,
        minNodes: 1,
        maxNodes: 4,
        sku: "4xl40s-48",
        topologyKind: "pcie",
        bandwidth: 64,
        nodes: [
          { suffix: "n01", condition: "ready", cordoned: false, devices: 4, unhealthy: 0 },
          { suffix: "n02", condition: "ready", cordoned: false, devices: 4, unhealthy: 1 },
          { suffix: "n03", condition: "unknown", cordoned: false, devices: 4, unhealthy: 4 },
        ],
      },
    ],
  },
  {
    name: "ams-1",
    displayName: "Amsterdam 1",
    region: "eu-west-1",
    zone: "euw1-az1",
    residency: "eu",
    phase: "draining",
    reachable: true,
    labels: { tier: "prod", residency: "eu" },
    capabilities: { nvlink: true, rdma: true },
    syncState: "connected",
    agentVersion: "0.4.2",
    lastError: null,
    enrolled: true,
    pools: [
      {
        name: "a100-batch",
        deviceClass: "a100-80",
        devicesPerNode: 8,
        minNodes: 2,
        maxNodes: 6,
        sku: "8xa100-80",
        topologyKind: "nvlink",
        bandwidth: 600,
        nodes: [
          { suffix: "n01", condition: "ready", cordoned: true, devices: 8, unhealthy: 0 },
          { suffix: "n02", condition: "ready", cordoned: true, devices: 8, unhealthy: 2 },
        ],
      },
    ],
  },
  {
    name: "fra-1",
    displayName: null,
    region: "eu-central-1",
    zone: null,
    residency: "eu",
    phase: "pending",
    reachable: false,
    labels: { tier: "staging" },
    capabilities: {},
    syncState: "never_connected",
    agentVersion: null,
    lastError: null,
    enrolled: false,
    pools: [
      {
        name: "h200-staging",
        deviceClass: "h200-141",
        devicesPerNode: 8,
        minNodes: 2,
        maxNodes: 8,
        sku: "8xh200-141",
        topologyKind: "nvswitch",
        bandwidth: 900,
        nodes: [],
      },
    ],
  },
]

const DEVICE_MEMORY_MIB: Record<string, number> = {
  "h100-80": 81_920,
  "l40s-48": 49_152,
  "a100-80": 81_920,
  "h200-141": 144_384,
}

export const clusters: Cluster[] = []
export const pools: Pool[] = []
export const nodes: SupplyNode[] = []
export const devices: Device[] = []
export const domains: TopologyDomain[] = []

for (const spec of SPECS) {
  const clusterId = uid("clu_")
  clusters.push({
    id: clusterId,
    name: spec.name,
    display_name: spec.displayName,
    region: spec.region,
    zone: spec.zone,
    residency: spec.residency,
    phase: spec.phase,
    labels: spec.labels,
    capabilities: spec.capabilities,
    enrolled_at: spec.enrolled ? iso(172_800) : null,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    reachable: spec.reachable,
    sync: {
      state: spec.syncState,
      last_sync_at: spec.enrolled ? iso(spec.reachable ? 0.2 : 47) : null,
      last_seen_at: spec.enrolled ? iso(spec.reachable ? 0.1 : 46) : null,
      agent_version: spec.agentVersion,
      lag_ms: spec.reachable ? 340 : null,
      last_error: spec.lastError,
    },
  })

  for (const pool of spec.pools) {
    const poolId = uid("pol_")
    const membership = {
      nodes_total: 0,
      nodes_ready: 0,
      nodes_cordoned: 0,
      devices_total: 0,
      devices_healthy: 0,
      devices_allocatable: 0,
    }

    for (const node of pool.nodes) {
      const nodeId = uid("nod_")
      const nodeName = `${spec.name}-${node.suffix}`
      const domainId = uid("tpd_")
      const healthy = node.devices - node.unhealthy
      const byHealth: Record<string, number> = {}
      if (healthy > 0) byHealth.healthy = healthy
      if (node.unhealthy > 0) {
        byHealth[node.condition === "ready" ? "unhealthy" : "unknown"] = node.unhealthy
      }

      domains.push({
        id: domainId,
        cluster: clusterId,
        cluster_name: spec.name,
        node: nodeId,
        node_name: nodeName,
        parent: null,
        kind: pool.topologyKind,
        identifier: `${nodeName}/${pool.topologyKind}-0`,
        link_generation: pool.topologyKind === "nvswitch" ? "4" : null,
        link_bandwidth_gbps: pool.bandwidth,
        device_count: node.devices,
        spans_nodes: false,
        attributes: pool.topologyKind === "nvswitch" ? { switches: 4 } : {},
        observed_at: iso(1),
        created_at: CREATED,
        updated_at: null,
      })

      nodes.push({
        id: nodeId,
        name: nodeName,
        cluster: clusterId,
        cluster_name: spec.name,
        pool: poolId,
        pool_name: pool.name,
        condition: node.condition,
        cordoned: node.cordoned,
        ready: node.condition === "ready",
        cpu_cores: 192,
        memory_mib: 2_097_152,
        cpu_arch: "amd64",
        os_image: "Ubuntu 22.04.4 LTS",
        kernel_version: "5.15.0-119-generic",
        driver_version: "550.90.07",
        agent_version: spec.agentVersion,
        labels: spec.labels,
        taints: node.cordoned ? [{ key: "hangar.io/cordoned", effect: "NoSchedule" }] : [],
        devices: { total: node.devices, by_health: byHealth },
        joined_at: iso(43_200),
        observed_at: iso(1),
        created_at: CREATED,
        updated_at: null,
      })

      for (let index = 0; index < node.devices; index++) {
        const unhealthy = index >= healthy
        const health = unhealthy
          ? node.condition === "ready"
            ? "unhealthy"
            : "unknown"
          : "healthy"
        devices.push({
          id: uid("dev_"),
          cluster: clusterId,
          cluster_name: spec.name,
          node: nodeId,
          node_name: nodeName,
          device_uuid: `GPU-${uuid(devices.length + 1)}`,
          device_index: index,
          device_class: pool.deviceClass,
          vendor: "nvidia",
          model_name: pool.deviceClass.toUpperCase(),
          pci_address: `0000:${(index + 1).toString(16).padStart(2, "0")}:00.0`,
          memory_mib: DEVICE_MEMORY_MIB[pool.deviceClass] ?? 81_920,
          compute_capability: "9.0",
          health,
          health_reason: health === "unhealthy" ? "ecc uncorrectable error" : null,
          ecc_enabled: true,
          ecc_volatile_errors: health === "unhealthy" ? 4 : 0,
          ecc_aggregate_errors: health === "unhealthy" ? 137 : 0,
          ecc_pending_retirement: health === "unhealthy",
          throttled: health === "unhealthy",
          throttle_reasons: health === "unhealthy" ? ["sw_thermal_slowdown"] : [],
          mig_enabled: false,
          mig_profile: null,
          mig_parent_device_uuid: null,
          allocatable: health === "healthy" && !node.cordoned,
          topology: {
            id: domainId,
            kind: pool.topologyKind,
            identifier: `${nodeName}/${pool.topologyKind}-0`,
          },
          observed_at: iso(1),
          created_at: CREATED,
          updated_at: null,
        })
      }

      membership.nodes_total += 1
      if (node.condition === "ready") membership.nodes_ready += 1
      if (node.cordoned) membership.nodes_cordoned += 1
      membership.devices_total += node.devices
      membership.devices_healthy += healthy
      membership.devices_allocatable += node.cordoned ? 0 : healthy
    }

    pools.push({
      id: poolId,
      cluster_id: clusterId,
      name: pool.name,
      device_class: pool.deviceClass,
      devices_per_node: pool.devicesPerNode,
      node_shape: { sku: pool.sku, cpu: 192, memory_gib: 2048 },
      min_nodes: pool.minNodes,
      max_nodes: pool.maxNodes,
      labels: spec.labels,
      taints: [],
      enabled: spec.phase !== "disabled",
      created_at: CREATED,
      updated_at: null,
      revision: REV,
      membership,
      within_declared_bounds:
        membership.nodes_total >= pool.minNodes &&
        membership.nodes_total <= pool.maxNodes,
    })
  }
}

const tenant = {
  id: uid("ten_"),
  slug: "acme",
  name: "Acme",
  enabled: true,
}

export const priorityClasses: PriorityClass[] = [
  {
    id: uid("prc_"),
    name: "production",
    value: 100,
    preemptible: false,
    preempts: true,
    is_default: true,
    description: "Serving traffic. Takes capacity, never gives it up.",
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
  {
    id: uid("prc_"),
    name: "batch",
    value: 10,
    preemptible: true,
    preempts: false,
    is_default: false,
    description: null,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
]

export const projects: Project[] = [
  {
    id: uid("prj_"),
    tenant,
    slug: "core",
    name: "Core serving",
    default_priority_class: "production",
    enabled: true,
    effective_enabled: true,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
  {
    id: uid("prj_"),
    tenant,
    slug: "research",
    name: "Research",
    default_priority_class: "batch",
    enabled: false,
    effective_enabled: false,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
]

export const artifacts: Artifact[] = [
  {
    id: uid("art_"),
    digest: "sha256:9f2c1a0b7d4e5f6a8b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c",
    name: "llama-3.3-70b-instruct",
    kind: "weights",
    format: "safetensors",
    size_bytes: 141_733_920_768,
    licence: "llama3.3",
    parameter_count: 70_000_000_000,
    base_artifact_id: null,
    project_id: null,
    metadata: { source: "huggingface" },
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    locations: [
      {
        scheme: "s3",
        uri: "s3://hangar-weights/llama-3.3-70b-instruct",
        region: "us-east-1",
        preferred: true,
      },
      { scheme: "https", uri: "https://cdn.hangar.dev/llama-3.3-70b", region: null, preferred: false },
    ],
  },
  {
    id: uid("art_"),
    digest: "sha256:1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6a8b9c0d1e2f3a4b5c6d7e",
    name: "bge-m3-embed",
    kind: "weights",
    format: "safetensors",
    size_bytes: 2_271_560_481,
    licence: "mit",
    parameter_count: 568_000_000,
    base_artifact_id: null,
    project_id: null,
    metadata: {},
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    locations: [],
  },
]

export const recipes: Recipe[] = [
  {
    id: uid("rec_"),
    artifact_id: artifacts[0].id,
    name: "llama-70b-fp8-tp8",
    version: 3,
    engine: "vllm",
    engine_version: "0.9.1",
    engine_image: "ghcr.io/hangar/vllm:0.9.1",
    engine_args: { "--enable-chunked-prefill": true },
    quantization: "fp8",
    dtype: "bfloat16",
    parallelism: { tensor: 8, pipeline: 1, expert: 1 },
    context_length: 131_072,
    max_batch_tokens: 32_768,
    cache: {
      kv_cache_dtype: "fp8",
      kv_cache_fraction: 0.9,
      prefix_cache_enabled: true,
      prefix_cache_config: { blocks: 4096 },
    },
    shape: {
      device_class: "h100-80",
      device_count: 8,
      device_memory_mib_min: 81_920,
      requires_single_topology_domain: true,
      topology_domain_kind: "nvswitch",
    },
    resources: { cpu_cores: 64, memory_mib: 262_144, disk_gib: 512 },
    labels: { tier: "prod" },
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
  {
    id: uid("rec_"),
    artifact_id: artifacts[1].id,
    name: "bge-m3-l40s",
    version: 1,
    engine: "vllm",
    engine_version: "0.9.1",
    engine_image: null,
    engine_args: {},
    quantization: null,
    dtype: "float16",
    parallelism: { tensor: 1, pipeline: 1, expert: 1 },
    context_length: 8192,
    max_batch_tokens: null,
    cache: {
      kv_cache_dtype: null,
      kv_cache_fraction: null,
      prefix_cache_enabled: false,
      prefix_cache_config: {},
    },
    shape: {
      device_class: "l40s-48",
      device_count: 1,
      device_memory_mib_min: 0,
      requires_single_topology_domain: false,
      topology_domain_kind: null,
    },
    resources: { cpu_cores: null, memory_mib: null, disk_gib: null },
    labels: {},
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
]

export const deployments: Deployment[] = [
  {
    id: uid("dep_"),
    name: "llama-70b-prod",
    cluster_id: clusters[0].id,
    recipe_id: recipes[0].id,
    min_replicas: 2,
    max_replicas: 6,
    desired_replicas: 4,
    generation: 7,
    enabled: true,
    adapter_artifact_ids: [],
    overrides: { "--max-num-seqs": 256 },
    labels: { tier: "prod" },
    created_at: CREATED,
    updated_at: iso(12),
    revision: REV,
    workload: {
      type: "service",
      phase: "running",
      reason: null,
      project_id: projects[0].id,
      priority_class: "production",
      preemptible: false,
      gang_size: 8,
      queued_at: iso(2880),
      admitted_at: iso(2879),
      placed_at: iso(2878),
      started_at: iso(2875),
      deadline_at: null,
    },
    replicas: {
      total: 4,
      ready: 3,
      running: 4,
      pending: 0,
      failed: 0,
      at_current_generation: 3,
    },
  },
  {
    id: uid("dep_"),
    name: "bge-embed",
    cluster_id: clusters[1].id,
    recipe_id: recipes[1].id,
    min_replicas: 1,
    max_replicas: 3,
    desired_replicas: 2,
    generation: 1,
    enabled: true,
    adapter_artifact_ids: [],
    overrides: {},
    labels: {},
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    workload: {
      type: "service",
      phase: "placed",
      reason: null,
      project_id: projects[0].id,
      priority_class: "production",
      preemptible: false,
      gang_size: 1,
      queued_at: iso(90),
      admitted_at: iso(89),
      placed_at: iso(88),
      started_at: null,
      deadline_at: null,
    },
    replicas: {
      total: 2,
      ready: 0,
      running: 1,
      pending: 1,
      failed: 0,
      at_current_generation: 2,
    },
  },
  {
    id: uid("dep_"),
    name: "nightly-eval",
    cluster_id: clusters[2].id,
    recipe_id: recipes[0].id,
    min_replicas: 1,
    max_replicas: 1,
    desired_replicas: 1,
    generation: 2,
    enabled: false,
    adapter_artifact_ids: [artifacts[1].id],
    overrides: {},
    labels: { tier: "batch" },
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    workload: {
      type: "gang",
      phase: "pending",
      reason: "no topology domain with 8 free devices",
      project_id: projects[1].id,
      priority_class: "batch",
      preemptible: true,
      gang_size: 8,
      queued_at: iso(30),
      admitted_at: null,
      placed_at: null,
      started_at: null,
      deadline_at: iso(-360),
    },
    replicas: {
      total: 0,
      ready: 0,
      running: 0,
      pending: 0,
      failed: 0,
      at_current_generation: 0,
    },
  },
]

export const endpoints: Endpoint[] = [
  {
    id: uid("end_"),
    name: "chat",
    display_name: "Chat completions",
    project_id: projects[0].id,
    visibility: "public",
    routing_strategy: "weighted",
    enabled: true,
    targets: [
      { deployment_id: deployments[0].id, weight: 90, role: "primary", enabled: true },
      { deployment_id: deployments[1].id, weight: 10, role: "canary", enabled: true },
    ],
    failover_order: [deployments[0].id, deployments[1].id],
    labels: { tier: "prod" },
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
  {
    id: uid("end_"),
    name: "embeddings",
    display_name: null,
    project_id: projects[0].id,
    visibility: "private",
    routing_strategy: "weighted",
    enabled: true,
    targets: [
      { deployment_id: deployments[1].id, weight: 100, role: "primary", enabled: true },
    ],
    failover_order: [],
    labels: {},
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
]

export const quotas: Quota[] = [
  {
    id: uid("qta_"),
    project: projects[0].id,
    project_slug: "core",
    cluster: clusters[0].id,
    cluster_name: clusters[0].name,
    device_class: "h100-80",
    guaranteed_devices: 16,
    limit_devices: 32,
    over_quota_weight: 2,
    over_quota_enabled: true,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    usage: { allocated_devices: 24, live_allocations: 3, over_quota_devices: 8 },
    headroom_devices: 8,
    over_limit: false,
  },
  {
    id: uid("qta_"),
    project: projects[1].id,
    project_slug: "research",
    cluster: null,
    cluster_name: null,
    device_class: "a100-80",
    guaranteed_devices: 8,
    limit_devices: 8,
    over_quota_weight: 1,
    over_quota_enabled: false,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    usage: { allocated_devices: 10, live_allocations: 2, over_quota_devices: 2 },
    headroom_devices: -2,
    over_limit: true,
  },
]

export const policies: Policy[] = [
  {
    id: uid("plc_"),
    name: "eu-residency",
    description: "EU customer data never leaves the EU.",
    allowed_regions: ["eu-west-1", "eu-central-1"],
    allowed_residencies: ["eu"],
    allowed_device_classes: [],
    allowed_clusters: [],
    denied_clusters: [clusters[1].id],
    max_hourly_cost: 240,
    rules: {},
    enabled: true,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    attachments: [
      {
        subject_type: "project",
        subject_id: uuid(91),
        subject: projects[1].id,
        subject_name: "research",
      },
    ],
  },
]

const liveDevices = devices.filter((d) => d.allocatable).slice(0, 12)

export const allocations: Allocation[] = [
  {
    id: uid("alc_"),
    cluster: clusters[0].id,
    cluster_name: clusters[0].name,
    project: projects[0].id,
    project_slug: "core",
    workload: uid("wkl_"),
    replica: uid("rep_"),
    device_class: "h100-80",
    device_count: 8,
    over_quota: false,
    phase: "held",
    live: true,
    gang_complete: true,
    acquired_at: iso(2875),
    released_at: null,
    observed_at: iso(1),
    created_at: CREATED,
    updated_at: null,
    devices: liveDevices.slice(0, 8).map((d) => ({
      device: d.id,
      device_uuid: d.device_uuid,
      node: d.node,
      live: true,
      released_at: null,
    })),
  },
  {
    id: uid("alc_"),
    cluster: clusters[1].id,
    cluster_name: clusters[1].name,
    project: projects[0].id,
    project_slug: "core",
    workload: uid("wkl_"),
    replica: null,
    device_class: "l40s-48",
    device_count: 4,
    over_quota: true,
    phase: "held",
    live: true,
    gang_complete: false,
    acquired_at: iso(88),
    released_at: null,
    observed_at: iso(1),
    created_at: CREATED,
    updated_at: null,
    devices: liveDevices.slice(8, 11).map((d) => ({
      device: d.id,
      device_uuid: d.device_uuid,
      node: d.node,
      live: true,
      released_at: null,
    })),
  },
  {
    id: uid("alc_"),
    cluster: clusters[2].id,
    cluster_name: clusters[2].name,
    project: projects[1].id,
    project_slug: "research",
    workload: uid("wkl_"),
    replica: null,
    device_class: "a100-80",
    device_count: 8,
    over_quota: false,
    phase: "released",
    live: false,
    gang_complete: false,
    acquired_at: iso(600),
    released_at: iso(120),
    observed_at: iso(120),
    created_at: CREATED,
    updated_at: iso(120),
    devices: [],
  },
]

export const components: Component[] = [
  {
    id: uid("cmp_"),
    cluster_id: clusters[0].id,
    name: "operator",
    kind: "operator",
    desired_version: "0.4.2",
    config: {},
    enabled: true,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    status: {
      observed_version: "0.4.2",
      rollout_phase: "ready",
      healthy: true,
      message: null,
      observed_at: iso(1),
    },
    behind: false,
  },
  {
    id: uid("cmp_"),
    cluster_id: clusters[0].id,
    name: "scheduler",
    kind: "scheduler",
    desired_version: "1.4.0",
    config: { binpack: true },
    enabled: true,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    status: {
      observed_version: "1.3.2",
      rollout_phase: "rolling",
      healthy: true,
      message: "2 of 3 replicas upgraded",
      observed_at: iso(3),
    },
    behind: true,
  },
  {
    id: uid("cmp_"),
    cluster_id: clusters[1].id,
    name: "operator",
    kind: "operator",
    desired_version: "0.4.2",
    config: {},
    enabled: true,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    status: {
      observed_version: null,
      rollout_phase: "unknown",
      healthy: false,
      message: null,
      observed_at: null,
    },
    behind: true,
  },
]

const drainNode = nodes[2]

export const operations: Operation[] = [
  {
    id: uid("opr_"),
    cluster_id: clusters[0].id,
    type: "node.drain",
    target: { type: "node", id: drainNode.id },
    reason: "firmware",
    requested_by: "operator",
    parameters: { grace_seconds: 900 },
    deadline_at: iso(-60),
    created_at: iso(18),
    updated_at: iso(2),
    phase: "running",
    result: null,
    error: null,
    attempts: 1,
    cancel_requested_at: null,
    started_at: iso(17),
    finished_at: null,
  },
  {
    id: uid("opr_"),
    cluster_id: clusters[0].id,
    type: "device.reset",
    target: { type: "device", id: devices[7].id },
    reason: "ecc uncorrectable",
    requested_by: "operator",
    parameters: {},
    deadline_at: null,
    created_at: iso(240),
    updated_at: iso(238),
    phase: "succeeded",
    result: { reset: true },
    error: null,
    attempts: 1,
    cancel_requested_at: null,
    started_at: iso(239),
    finished_at: iso(238),
  },
  {
    id: uid("opr_"),
    cluster_id: clusters[1].id,
    type: "component.upgrade",
    target: { type: "component", id: components[2].id },
    reason: null,
    requested_by: "operator",
    parameters: { version: "0.4.2" },
    deadline_at: null,
    created_at: iso(75),
    updated_at: iso(70),
    phase: "failed",
    result: null,
    error: "cluster unreachable: no sync session",
    attempts: 3,
    cancel_requested_at: null,
    started_at: iso(74),
    finished_at: iso(70),
  },
  {
    id: uid("opr_"),
    cluster_id: clusters[2].id,
    type: "cache.warm",
    target: { type: "artifact", id: artifacts[0].id },
    reason: "pre-stage for rollout",
    requested_by: "operator",
    parameters: {},
    deadline_at: null,
    created_at: iso(9),
    updated_at: null,
    phase: "pending",
    result: null,
    error: null,
    attempts: 0,
    cancel_requested_at: null,
    started_at: null,
    finished_at: null,
  },
]

export const events: PlaneEvent[] = [
  {
    id: uuid(1),
    cluster_id: null,
    kind: "plane.started",
    severity: "info",
    subject: { type: "plane", id: null },
    operation_id: null,
    message: "control plane started",
    details: { version: "0.1.0" },
    occurred_at: iso(300),
    recorded_at: iso(300),
  },
  {
    id: uuid(2),
    cluster_id: clusters[0].id,
    kind: "cluster.enrolled",
    severity: "info",
    subject: { type: "cluster", id: uuid(20) },
    operation_id: null,
    message: "iad-1 enrolled",
    details: {},
    occurred_at: iso(172_800),
    recorded_at: iso(172_800),
  },
  {
    id: uuid(3),
    cluster_id: clusters[0].id,
    kind: "device.unhealthy",
    severity: "warning",
    subject: { type: "device", id: uuid(21) },
    operation_id: null,
    message: "iad-1-n01 device 7 reported an uncorrectable ECC error",
    details: { ecc_aggregate_errors: 137 },
    occurred_at: iso(245),
    recorded_at: iso(245),
  },
  {
    id: uuid(4),
    cluster_id: clusters[0].id,
    kind: "node.drain.started",
    severity: "info",
    subject: { type: "node", id: uuid(22) },
    operation_id: operations[0].id,
    message: `${drainNode.name} drain started`,
    details: { reason: "firmware" },
    occurred_at: iso(17),
    recorded_at: iso(17),
  },
  {
    id: uuid(5),
    cluster_id: clusters[1].id,
    kind: "cluster.sync.lost",
    severity: "error",
    subject: { type: "cluster", id: uuid(23) },
    operation_id: null,
    message: "sjc-1 sync stream closed by peer",
    details: { last_error: "stream closed by peer" },
    occurred_at: iso(47),
    recorded_at: iso(47),
  },
  {
    id: uuid(6),
    cluster_id: clusters[2].id,
    kind: "workload.admission.rejected",
    severity: "warning",
    subject: { type: "workload", id: uuid(24) },
    operation_id: null,
    message: "nightly-eval could not be admitted",
    details: { reason: "no topology domain with 8 free devices" },
    occurred_at: iso(29),
    recorded_at: iso(29),
  },
]
