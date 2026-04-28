import type { Cluster } from "@/modules/clusters/types"
import type {
  Artifact,
  Deployment,
  Endpoint,
  Recipe,
  Workload,
  WorkloadDeployment,
  WorkloadScale,
} from "@/modules/demand/types"
import {
  emptyCapabilities,
  emptyServing,
  emptySurface,
  emptyUpstream,
} from "@/modules/demand/display"
import type {
  Policy,
  PriorityClass,
  Project,
  Quota,
  Tenant,
} from "@/modules/binding/types"
import type {
  Allocation,
  Component,
  Operation,
  PlaneEvent,
} from "@/modules/machinery/types"
import type {
  Device,
  DeviceClass,
  Pool,
  SupplyNode,
  TopologyDomain,
} from "@/modules/supply/types"
import type { DiscoveredWorkload } from "@/modules/discovered/types"
import type { BenchmarkRun } from "@/modules/benchmarks/types"
import { revision, uid, uuid } from "./ids"

/**
 * The offline corpus. Every row is typed as the schema the plane actually
 * returns, so a field the server does not send cannot be invented here and a
 * field it does send cannot be forgotten — a fixture that disagrees with the
 * server is worse than no fixture, because it makes a broken client's tests
 * pass.
 */

const NOW = Date.parse("2026-08-19T18:00:00.000Z")

const iso = (minutesAgo: number) =>
  new Date(NOW - minutesAgo * 60_000).toISOString()

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
  operatorVersion: string | null
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
    operatorVersion: "0.4.2",
    lastError: null,
    enrolled: true,
    pools: [
      {
        name: "h100-prod",
        deviceClass: "h100-80gb-sxm",
        devicesPerNode: 8,
        minNodes: 4,
        maxNodes: 16,
        sku: "8xh100-80",
        topologyKind: "nvswitch",
        bandwidth: 900,
        nodes: [
          {
            suffix: "n01",
            condition: "ready",
            cordoned: false,
            devices: 8,
            unhealthy: 1,
          },
          {
            suffix: "n02",
            condition: "ready",
            cordoned: false,
            devices: 8,
            unhealthy: 0,
          },
          {
            suffix: "n03",
            condition: "ready",
            cordoned: true,
            devices: 8,
            unhealthy: 0,
          },
          {
            suffix: "n04",
            condition: "not_ready",
            cordoned: true,
            devices: 8,
            unhealthy: 8,
          },
          {
            suffix: "n05",
            condition: "ready",
            cordoned: false,
            devices: 8,
            unhealthy: 0,
          },
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
    operatorVersion: "0.4.1",
    lastError: "stream closed by peer",
    enrolled: true,
    pools: [
      {
        name: "l40s-serving",
        deviceClass: "l40s-48gb-pcie",
        devicesPerNode: 4,
        minNodes: 1,
        maxNodes: 4,
        sku: "4xl40s-48",
        topologyKind: "pcie",
        bandwidth: 64,
        nodes: [
          {
            suffix: "n01",
            condition: "ready",
            cordoned: false,
            devices: 4,
            unhealthy: 0,
          },
          {
            suffix: "n02",
            condition: "ready",
            cordoned: false,
            devices: 4,
            unhealthy: 1,
          },
          {
            suffix: "n03",
            condition: "unknown",
            cordoned: false,
            devices: 4,
            unhealthy: 4,
          },
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
    operatorVersion: "0.4.2",
    lastError: null,
    enrolled: true,
    pools: [
      {
        name: "a100-batch",
        deviceClass: "a100-80gb-sxm",
        devicesPerNode: 8,
        minNodes: 2,
        maxNodes: 6,
        sku: "8xa100-80",
        topologyKind: "nvlink",
        bandwidth: 600,
        nodes: [
          {
            suffix: "n01",
            condition: "ready",
            cordoned: true,
            devices: 8,
            unhealthy: 0,
          },
          {
            suffix: "n02",
            condition: "ready",
            cordoned: false,
            devices: 8,
            unhealthy: 2,
          },
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
    operatorVersion: null,
    lastError: null,
    enrolled: false,
    pools: [
      {
        name: "h200-staging",
        deviceClass: "h200-141gb-sxm",
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

/**
 * The catalog half of `/device-classes`, copied from
 * `libs/packages/contract/devices.json` for the classes this fixture seeds.
 *
 * Values are the contract's, not invented ones: the fixture exists so the
 * console can be worked on without a plane, and a fixture that disagrees with
 * the contract makes a broken console look right. `rtx-6000-ada-48gb-pcie`
 * carries no devices on purpose — hardware we could take delivery of but do not
 * own is half of what the catalog is for.
 */
const CATALOG: Omit<DeviceClass, "inventory">[] = [
  {
    name: "h100-80gb-sxm",
    label: "NVIDIA H100 80GB SXM",
    architecture: "hopper",
    vendor: "nvidia",
    memory_mib: 81_559,
    form_factor: "sxm",
    in_catalog: true,
    aliases: {
      gfd: ["NVIDIA-H100-80GB-HBM3"],
      dcgm: ["NVIDIA H100 80GB HBM3"],
    },
  },
  {
    name: "h200-141gb-sxm",
    label: "NVIDIA H200 141GB SXM",
    architecture: "hopper",
    vendor: "nvidia",
    memory_mib: 143_771,
    form_factor: "sxm",
    in_catalog: true,
    aliases: { gfd: ["NVIDIA-H200"], dcgm: ["NVIDIA H200"] },
  },
  {
    name: "b200-180gb-sxm",
    label: "NVIDIA B200 180GB SXM",
    architecture: "blackwell",
    vendor: "nvidia",
    memory_mib: 183_359,
    form_factor: "sxm",
    in_catalog: true,
    aliases: { gfd: ["NVIDIA-B200"], dcgm: ["NVIDIA B200"] },
  },
  {
    name: "b300-288gb-sxm",
    label: "NVIDIA B300 288GB SXM",
    architecture: "blackwell",
    vendor: "nvidia",
    memory_mib: 294_912,
    form_factor: "sxm",
    in_catalog: true,
    aliases: {
      gfd: ["NVIDIA-B300-SXM6-AC"],
      dcgm: ["NVIDIA B300 SXM6 AC"],
    },
  },
  {
    name: "gb200-186gb-nvl",
    label: "NVIDIA GB200 NVL72",
    architecture: "blackwell",
    vendor: "nvidia",
    memory_mib: 190_464,
    form_factor: "nvl",
    in_catalog: true,
    aliases: {
      gfd: ["NVIDIA-GB200-NVL72", "NVIDIA-GB200"],
      dcgm: ["NVIDIA GB200 NVL72", "NVIDIA GB200"],
    },
  },
  {
    name: "gb300-288gb-nvl",
    label: "NVIDIA GB300 NVL72",
    architecture: "blackwell",
    vendor: "nvidia",
    memory_mib: 294_912,
    form_factor: "nvl",
    in_catalog: true,
    aliases: {
      gfd: ["NVIDIA-GB300-NVL72", "NVIDIA-GB300"],
      dcgm: ["NVIDIA GB300 NVL72", "NVIDIA GB300"],
    },
  },
  {
    name: "a100-80gb-sxm",
    label: "NVIDIA A100 80GB SXM",
    architecture: "ampere",
    vendor: "nvidia",
    memory_mib: 81_920,
    form_factor: "sxm",
    in_catalog: true,
    aliases: {
      gfd: ["NVIDIA-A100-SXM4-80GB"],
      dcgm: ["NVIDIA A100-SXM4-80GB"],
    },
  },
  {
    name: "l40s-48gb-pcie",
    label: "NVIDIA L40S 48GB",
    architecture: "ada",
    vendor: "nvidia",
    memory_mib: 46_068,
    form_factor: "pcie",
    in_catalog: true,
    aliases: { gfd: ["NVIDIA-L40S"], dcgm: ["NVIDIA L40S"] },
  },
  {
    name: "rtx-6000-ada-48gb-pcie",
    label: "NVIDIA RTX 6000 Ada 48GB",
    architecture: "ada",
    vendor: "nvidia",
    memory_mib: 49_140,
    form_factor: "pcie",
    in_catalog: true,
    aliases: { gfd: ["NVIDIA-RTX-6000-Ada-Generation"] },
  },
]

const DEVICE_MEMORY_MIB: Record<string, number> = Object.fromEntries(
  CATALOG.map((entry) => [entry.name, entry.memory_mib])
)

/**
 * The catalog joined to the seeded fleet, the way the plane joins it on read.
 *
 * `sm-9000` is here and is not in the catalog: it stands for hardware the
 * contract table has never heard of, which resolved to the slug of its vendor
 * string and is being scheduled onto anyway. The console has to render that
 * case, so the fixture has to produce it.
 */
export function deviceClasses(cluster?: string): DeviceClass[] {
  const scoped =
    cluster === undefined
      ? devices
      : devices.filter((device) => device.cluster === cluster)
  const names = new Set([
    ...CATALOG.map((entry) => entry.name),
    ...scoped.map((device) => device.device_class),
  ])

  return [...names]
    .map((name) => {
      const entry = CATALOG.find((c) => c.name === name)
      const owned = scoped.filter((device) => device.device_class === name)
      return {
        name,
        label: entry?.label ?? name,
        architecture: entry?.architecture ?? "",
        vendor: entry?.vendor ?? "",
        memory_mib: entry?.memory_mib ?? 0,
        form_factor: entry?.form_factor ?? "",
        in_catalog: entry !== undefined,
        aliases: entry?.aliases ?? {},
        inventory: {
          devices: owned.length,
          healthy: owned.filter((d) => d.health === "healthy").length,
          allocatable: owned.filter((d) => d.allocatable).length,
          nodes: new Set(owned.map((d) => d.node)).size,
          clusters: [...new Set(owned.map((d) => d.cluster_name))].sort(),
        },
      }
    })
    .sort(
      (a, b) =>
        b.inventory.devices - a.inventory.devices ||
        a.name.localeCompare(b.name)
    )
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
    metrics_label: spec.enrolled ? spec.name : null,
    serving: spec.enrolled
      ? {
          url: `http://dynamo-frontend.${spec.name}.svc.cluster.local:8000`,
          credential_id: null,
          enabled: true,
        }
      : null,
    sync: {
      state: spec.syncState,
      last_sync_at: spec.enrolled ? iso(spec.reachable ? 0.2 : 47) : null,
      last_seen_at: spec.enrolled ? iso(spec.reachable ? 0.1 : 46) : null,
      distribution: spec.enrolled ? "eks" : null,
      cloud: spec.enrolled ? "aws" : null,
      observed_region: spec.enrolled ? spec.region : null,
      operator_version: spec.operatorVersion,
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
        byHealth[node.condition === "ready" ? "unhealthy" : "unknown"] =
          node.unhealthy
      }

      domains.push({
        id: domainId,
        cluster: clusterId,
        cluster_name: spec.name,
        nodes: [nodeId],
        node_names: [nodeName],
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
        conditions: [],
        cordoned: node.cordoned,
        ready: node.condition === "ready",
        instance_type:
          pool.topologyKind === "nvswitch" ? "p5.48xlarge" : "g6e.12xlarge",
        cloud: "aws",
        zone: spec.zone ?? null,
        region: spec.region,
        cpu_cores: 192,
        memory_mib: 2_097_152,
        cpu_arch: "amd64",
        os_image: "Ubuntu 22.04.4 LTS",
        kernel_version: "5.15.0-119-generic",
        driver_version: "550.90.07",
        operator_version: spec.operatorVersion,
        labels: spec.labels,
        taints: node.cordoned
          ? [{ key: "hangar.io/cordoned", effect: "NoSchedule" }]
          : [],
        devices: {
          total: node.devices,
          by_health: byHealth,
          device_class: pool.deviceClass,
          allocated: 0,
        },
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
          health_reason:
            health === "unhealthy" ? "ecc uncorrectable error" : null,
          ecc_enabled: true,
          ecc_volatile_errors: health === "unhealthy" ? 4 : 0,
          ecc_aggregate_errors: health === "unhealthy" ? 137 : 0,
          ecc_pending_retirement: health === "unhealthy",
          throttled: health === "unhealthy",
          throttle_reasons:
            health === "unhealthy" ? ["sw_thermal_slowdown"] : [],
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

/**
 * One partitioned card, so the intra-device layer is not empty.
 *
 * MIG children are devices in their own right in the plane's model: their own
 * uuid, their own memory, their own allocation, and the card they were cut from
 * named in `mig_parent_device_uuid`. The parent stops being schedulable the
 * moment it is split, which is why its `allocatable` goes false here.
 */
const partitioned = devices.find(
  (device) => device.node_name === "iad-1-n02" && device.device_index === 7
)
if (partitioned) {
  partitioned.mig_enabled = true
  partitioned.allocatable = false
  for (let slice = 0; slice < 4; slice++) {
    devices.push({
      ...partitioned,
      id: uid("dev_"),
      device_uuid: `MIG-${uuid(900 + slice)}`,
      device_index: slice,
      memory_mib: 20_480,
      mig_enabled: true,
      mig_profile: "2g.20gb",
      mig_parent_device_uuid: partitioned.device_uuid,
      allocatable: true,
      pci_address: null,
    })
  }
}

const tenant = {
  id: uid("ten_"),
  slug: "acme",
  name: "Acme",
  enabled: true,
}

export const tenants: Tenant[] = [
  {
    ...tenant,
    project_count: 2,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
]

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

/**
 * Llama 3.3 70B's attention shape, as the plane persists it on an artifact at
 * resolve time. Real values from the published `config.json`, because the fixture's
 * fitment matrix does the plane's arithmetic and a made-up shape would put the wrong
 * card at the top of a table somebody is learning to read.
 *
 * `bge-m3-embed` deliberately has none, but does record the source it came from.
 * That is the shape of the artifact `POST /artifacts/:ref/refresh` exists for: one
 * registered before the plane extracted shapes, sizable only to a weights floor, and
 * one re-read away from a priced context window. The console has to render the floor
 * as a note rather than an empty table, and then render what the refresh changed.
 */
const LLAMA_70B_SHAPE = {
  num_hidden_layers: 80,
  num_key_value_heads: 8,
  head_dim: 128,
  kv_lora_rank: null,
  qk_rope_head_dim: null,
  context_length: 131_072,
  num_attention_heads: 64,
  hidden_size: 8192,
}

export const artifacts: Artifact[] = [
  {
    id: uid("art_"),
    digest:
      "sha256:9f2c1a0b7d4e5f6a8b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c",
    verified: true,
    name: "llama-3.3-70b-instruct",
    kind: "weights",
    format: "safetensors",
    size_bytes: 141_733_920_768,
    licence: "llama3.3",
    parameter_count: 70_000_000_000,
    base_artifact_id: null,
    project_id: null,
    upstream: emptyUpstream(),
    metadata: {
      source: "huggingface",
      quantization: "fp16",
      context_length: 131_072,
      shape: LLAMA_70B_SHAPE,
    },
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
      {
        scheme: "https",
        uri: "https://cdn.hangar.dev/llama-3.3-70b",
        region: null,
        preferred: false,
      },
    ],
  },
  {
    id: uid("art_"),
    digest:
      "sha256:1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6a8b9c0d1e2f3a4b5c6d7e",
    verified: true,
    name: "bge-m3-embed",
    kind: "weights",
    format: "safetensors",
    size_bytes: 2_271_560_481,
    licence: "mit",
    parameter_count: 568_000_000,
    base_artifact_id: null,
    project_id: null,
    upstream: emptyUpstream(),
    metadata: { source: "huggingface", huggingface_id: "BAAI/bge-m3" },
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
    lifecycle: "published",
    engine: "vllm",
    engine_version: "0.9.1",
    engine_image: "ghcr.io/hangar/vllm:0.9.1",
    engine_args: { "--enable-chunked-prefill": true },
    imported_args: {},
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
    serving: emptyServing(),
    capabilities: emptyCapabilities(),
    resolved_engine_args: {},
    shape: {
      device_class: "h100-80gb-sxm",
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
    lifecycle: "published",
    engine: "vllm",
    engine_version: "0.9.1",
    engine_image: null,
    engine_args: {},
    imported_args: {},
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
    serving: emptyServing(),
    capabilities: emptyCapabilities(),
    resolved_engine_args: {},
    shape: {
      device_class: "l40s-48gb-pcie",
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
  {
    id: uid("rec_"),
    artifact_id: artifacts[0].id,
    name: "llama-70b-a100-eval",
    version: 1,
    lifecycle: "published",
    engine: "vllm",
    engine_version: "0.9.1",
    engine_image: null,
    engine_args: {},
    imported_args: {},
    quantization: null,
    dtype: "bfloat16",
    parallelism: { tensor: 2, pipeline: 1, expert: 1 },
    context_length: 32_768,
    max_batch_tokens: 16_384,
    cache: {
      kv_cache_dtype: null,
      kv_cache_fraction: 0.8,
      prefix_cache_enabled: false,
      prefix_cache_config: {},
    },
    serving: emptyServing(),
    capabilities: emptyCapabilities(),
    resolved_engine_args: {},
    shape: {
      device_class: "a100-80gb-sxm",
      device_count: 2,
      device_memory_mib_min: 81_920,
      requires_single_topology_domain: true,
      topology_domain_kind: "nvlink",
    },
    resources: { cpu_cores: 32, memory_mib: 131_072, disk_gib: 256 },
    labels: { tier: "batch" },
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  },
]

/**
 * Workload ids are minted before the deployments so a deployment can name its
 * parent and the parent can list its shares. The workload is the object a user
 * creates; the deployments underneath it are what placement expanded it into.
 */
const workloadIds = {
  llama: uid("wkl_"),
  bge: uid("wkl_"),
  eval: uid("wkl_"),
}

export const deployments: Deployment[] = [
  {
    id: uid("dep_"),
    name: "llama-70b-prod",
    workload_id: workloadIds.llama,
    workload_name: "llama-70b-prod",
    cluster_id: clusters[0].id,
    min_replicas: 4,
    max_replicas: 4,
    desired_replicas: 4,
    admission_phase: "running",
    admission_reason: null,
    queued_at: iso(2880),
    admitted_at: iso(2879),
    placed_at: iso(2878),
    generation: 7,
    enabled: true,
    created_at: CREATED,
    updated_at: iso(12),
    revision: REV,
    adopted_workload_id: null,
    adopted_fingerprint: null,
    authority: "managed",
    status: {
      observed_generation: 7,
      phase: "degraded",
      reason: "Progressing",
      message: "3 of 4 replicas ready",
      desired_replicas: 4,
      ready_replicas: 3,
      updated_replicas: 3,
      unavailable_replicas: 1,
      last_error_code: null,
      last_error_message: null,
      last_error_at: null,
      observed_at: iso(1),
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
    name: "llama-70b-prod",
    workload_id: workloadIds.llama,
    workload_name: "llama-70b-prod",
    cluster_id: clusters[2].id,
    min_replicas: 2,
    max_replicas: 2,
    desired_replicas: 2,
    admission_phase: "placed",
    admission_reason: null,
    queued_at: iso(120),
    admitted_at: iso(119),
    placed_at: iso(118),
    generation: 2,
    enabled: true,
    created_at: iso(120),
    updated_at: iso(8),
    revision: revision(iso(120)),
    adopted_workload_id: null,
    adopted_fingerprint: null,
    authority: "managed",
    status: {
      observed_generation: 2,
      phase: "progressing",
      reason: "WeightsNotResident",
      message: "waiting for art_01j9x2eeee to finish downloading",
      desired_replicas: 2,
      ready_replicas: 1,
      updated_replicas: 1,
      unavailable_replicas: 1,
      last_error_code: null,
      last_error_message: null,
      last_error_at: null,
      observed_at: iso(1),
    },
    replicas: {
      total: 2,
      ready: 1,
      running: 2,
      pending: 0,
      failed: 0,
      at_current_generation: 1,
    },
  },
  {
    id: uid("dep_"),
    name: "bge-embed",
    workload_id: workloadIds.bge,
    workload_name: "bge-embed",
    cluster_id: clusters[1].id,
    min_replicas: 2,
    max_replicas: 2,
    desired_replicas: 2,
    admission_phase: "placed",
    admission_reason: null,
    queued_at: iso(90),
    admitted_at: iso(89),
    placed_at: iso(88),
    generation: 1,
    enabled: true,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    adopted_workload_id: null,
    adopted_fingerprint: null,
    authority: "managed",
    status: {
      observed_generation: 2,
      phase: "pending",
      reason: "WeightsNotResident",
      message: "waiting for art_01j9x2eeee to finish downloading",
      desired_replicas: 2,
      ready_replicas: 0,
      updated_replicas: 0,
      unavailable_replicas: 2,
      last_error_code: null,
      last_error_message: null,
      last_error_at: null,
      observed_at: iso(1),
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
    workload_id: workloadIds.eval,
    workload_name: "nightly-eval",
    cluster_id: clusters[2].id,
    min_replicas: 1,
    max_replicas: 1,
    desired_replicas: 1,
    admission_phase: "pending",
    admission_reason: "no topology domain with 8 free devices",
    queued_at: iso(30),
    admitted_at: null,
    placed_at: null,
    generation: 2,
    enabled: false,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    adopted_workload_id: null,
    adopted_fingerprint: null,
    authority: "managed",
    status: {
      observed_generation: 2,
      phase: "pending",
      reason: "NoClusterWork",
      message: "held as declared state; nothing to apply",
      desired_replicas: 1,
      ready_replicas: 0,
      updated_replicas: 0,
      unavailable_replicas: 1,
      last_error_code: null,
      last_error_message: null,
      last_error_at: null,
      observed_at: iso(30),
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

/** The unit the three scale numbers count, named by kind. */
const SCALE_UNIT: Record<string, string> = {
  service: "replicas",
  task: "completions",
  gang: "ranks",
}

/**
 * Projects a deployment into the row the workload carries for it, so the
 * per-cluster table and the deployment page cannot drift apart.
 */
function share(deployment: Deployment): WorkloadDeployment {
  const cluster = clusters.find((c) => c.id === deployment.cluster_id)!
  return {
    id: deployment.id,
    name: deployment.name,
    cluster_id: cluster.id,
    cluster_name: cluster.name,
    region: cluster.region,
    min_replicas: deployment.min_replicas,
    max_replicas: deployment.max_replicas,
    desired_replicas: deployment.desired_replicas,
    admission_phase: deployment.admission_phase,
    admission_reason: deployment.admission_reason,
    generation: deployment.generation,
    enabled: deployment.enabled,
    authority: deployment.authority,
    queued_at: deployment.queued_at,
    admitted_at: deployment.admitted_at,
    placed_at: deployment.placed_at,
    replicas: deployment.replicas,
    status: deployment.status,
  }
}

/**
 * `placed` and `ready` are sums over the shares — the one place summing is
 * legal, because it is the workload asking what its own expansion adds up to.
 */
function scale(
  kind: string,
  bounds: { min: number; max: number; desired: number },
  shares: WorkloadDeployment[]
): WorkloadScale {
  return {
    unit: SCALE_UNIT[kind] ?? "replicas",
    min: bounds.min,
    max: bounds.max,
    desired: bounds.desired,
    placed: shares.reduce((sum, s) => sum + s.desired_replicas, 0),
    ready: shares.reduce((sum, s) => sum + s.replicas.ready, 0),
  }
}

function workload(
  id: string,
  fields: Omit<
    Workload,
    "id" | "scale" | "deployments" | "recipe_name" | "artifact_tag"
  > & {
    bounds: { min: number; max: number; desired: number }
    recipe_name?: string | null
    artifact_tag?: string | null
  }
): Workload {
  const { bounds, recipe_name = null, artifact_tag = null, ...rest } = fields
  const shares = deployments
    .filter((d) => d.workload_id === id)
    .map((d) => share(d))
  return {
    ...rest,
    recipe_name,
    artifact_tag,
    id,
    scale: scale(rest.kind, bounds, shares),
    deployments: shares,
  }
}

export const workloads: Workload[] = [
  workload(workloadIds.llama, {
    name: "llama-70b-prod",
    kind: "service",
    phase: "placed",
    project_id: projects[0].id,
    priority_class: "production",
    preemptible: false,
    paused: false,
    gang_size: 1,
    recipe_id: recipes[0].id,
    adapter_artifact_ids: [],
    overrides: { "--max-num-seqs": 256 },
    labels: { tier: "prod" },
    requires_single_topology_domain: true,
    topology_domain_kind: "nvswitch",
    strategy: "spread",
    cluster_floors: { [clusters[2].id]: 2 },
    max_clusters: 2,
    pinned_cluster_id: null,
    bounds: { min: 2, max: 8, desired: 6 },
    started_at: iso(2875),
    finished_at: null,
    deadline_at: null,
    created_at: CREATED,
    updated_at: iso(12),
    revision: REV,
  }),
  workload(workloadIds.bge, {
    name: "bge-embed",
    kind: "service",
    phase: "placed",
    project_id: projects[0].id,
    priority_class: "production",
    preemptible: false,
    paused: false,
    gang_size: 1,
    recipe_id: recipes[1].id,
    adapter_artifact_ids: [],
    overrides: {},
    labels: {},
    requires_single_topology_domain: null,
    topology_domain_kind: null,
    strategy: "pack",
    cluster_floors: {},
    max_clusters: null,
    pinned_cluster_id: null,
    bounds: { min: 1, max: 3, desired: 2 },
    started_at: null,
    finished_at: null,
    deadline_at: null,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  }),
  workload(workloadIds.eval, {
    name: "nightly-eval",
    kind: "gang",
    phase: "pending",
    project_id: projects[1].id,
    priority_class: "batch",
    preemptible: true,
    paused: false,
    gang_size: 8,
    recipe_id: recipes[2].id,
    adapter_artifact_ids: [artifacts[1].id],
    overrides: {},
    labels: { tier: "batch" },
    requires_single_topology_domain: null,
    topology_domain_kind: null,
    strategy: "pack",
    cluster_floors: {},
    max_clusters: 1,
    pinned_cluster_id: clusters[2].id,
    bounds: { min: 8, max: 8, desired: 8 },
    started_at: null,
    finished_at: null,
    deadline_at: iso(-360),
    created_at: CREATED,
    updated_at: null,
    revision: REV,
  }),
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
      {
        deployment_id: deployments[0].id,
        weight: 70,
        role: "primary",
        enabled: true,
      },
      {
        deployment_id: deployments[1].id,
        weight: 30,
        role: "primary",
        enabled: true,
      },
    ],
    failover_order: [deployments[0].id, deployments[1].id],
    surface: emptySurface(),
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
      {
        deployment_id: deployments[2].id,
        weight: 100,
        role: "primary",
        enabled: true,
      },
    ],
    failover_order: [],
    surface: emptySurface(),
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
    device_class: "h100-80gb-sxm",
    guaranteed_devices: 16,
    limit_devices: 32,
    over_quota_weight: 2,
    over_quota_enabled: true,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    usage: {
      allocated_devices: 24,
      live_allocations: 3,
      over_quota_devices: 8,
    },
    headroom_devices: 8,
    over_limit: false,
  },
  {
    id: uid("qta_"),
    project: projects[1].id,
    project_slug: "research",
    cluster: null,
    cluster_name: null,
    device_class: "a100-80gb-sxm",
    guaranteed_devices: 8,
    limit_devices: 8,
    over_quota_weight: 1,
    over_quota_enabled: false,
    created_at: CREATED,
    updated_at: null,
    revision: REV,
    usage: {
      allocated_devices: 10,
      live_allocations: 2,
      over_quota_devices: 2,
    },
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

interface Commitment {
  /** The nodes it may take devices from, in the order it takes them. */
  nodes: string[]
  workload: string
  project: number
  count: number
  /** How far into each node's free devices to start. Leaves gaps on purpose. */
  offset?: number
  /** Hold MIG slices rather than whole cards. */
  slices?: boolean
  overQuota?: boolean
  age: number
}

const COMMITMENTS: Commitment[] = [
  {
    nodes: ["iad-1-n01"],
    workload: workloadIds.llama,
    project: 0,
    count: 8,
    age: 2875,
  },
  {
    nodes: ["iad-1-n02"],
    workload: workloadIds.llama,
    project: 0,
    count: 4,
    age: 1440,
  },
  {
    nodes: ["iad-1-n02"],
    workload: workloadIds.bge,
    project: 0,
    count: 2,
    slices: true,
    age: 200,
  },
  {
    nodes: ["sjc-1-n01"],
    workload: workloadIds.bge,
    project: 0,
    count: 3,
    overQuota: true,
    age: 88,
  },
  {
    nodes: ["ams-1-n02"],
    workload: workloadIds.eval,
    project: 1,
    count: 2,
    age: 640,
  },
  {
    nodes: ["ams-1-n02"],
    workload: workloadIds.eval,
    project: 1,
    count: 2,
    age: 620,
  },
  {
    nodes: ["sjc-1-n02"],
    workload: workloadIds.llama,
    project: 0,
    count: 2,
    offset: 2,
    age: 45,
  },
]

/**
 * Allocations built out of the devices they hold, rather than written beside
 * them.
 *
 * The device state is the one that has to agree: an allocation naming a device
 * the same fixture reports as allocatable is a state the plane cannot be in, and
 * a capacity board drawn over it would offer free devices that are not. So
 * holding a device here is what makes it unallocatable, in that order.
 */
function hold(commitment: Commitment): Allocation {
  const held: Device[] = []
  for (const name of commitment.nodes) {
    const open = devices
      .filter(
        (device) =>
          device.node_name === name &&
          device.allocatable &&
          (commitment.slices
            ? device.mig_parent_device_uuid !== null
            : device.mig_parent_device_uuid === null)
      )
      .sort((a, b) => a.device_index - b.device_index)
      .slice(commitment.offset ?? 0)
    for (const device of open) {
      if (held.length === commitment.count) break
      device.allocatable = false
      held.push(device)
    }
  }

  if (held.length === 0) {
    throw new Error(
      `offline fixture: nothing allocatable on ${commitment.nodes.join(", ")}`
    )
  }

  const cluster = clusters.find((entry) => entry.id === held[0].cluster)
  return {
    id: uid("alc_"),
    cluster: cluster?.id ?? clusters[0].id,
    cluster_name: cluster?.name ?? clusters[0].name,
    project: projects[commitment.project].id,
    project_slug: projects[commitment.project].slug,
    workload: commitment.workload,
    replica: uid("rep_"),
    device_class: held[0]?.device_class ?? "h100-80gb-sxm",
    device_count: commitment.count,
    over_quota: commitment.overQuota ?? false,
    phase: "held",
    live: true,
    gang_complete: held.length === commitment.count,
    acquired_at: iso(commitment.age),
    released_at: null,
    observed_at: iso(1),
    created_at: CREATED,
    updated_at: null,
    devices: held.map((device) => ({
      device: device.id,
      device_uuid: device.device_uuid,
      node: device.node,
      live: true,
      released_at: null,
    })),
  }
}

export const allocations: Allocation[] = [
  ...COMMITMENTS.map(hold),
  {
    id: uid("alc_"),
    cluster: clusters[2].id,
    cluster_name: clusters[2].name,
    project: projects[1].id,
    project_slug: "research",
    workload: workloadIds.eval,
    replica: null,
    device_class: "a100-80gb-sxm",
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

/** Rollups recounted once the commitments above have taken their devices. */
for (const node of nodes) {
  const own = devices.filter(
    (device) =>
      device.node === node.id && device.mig_parent_device_uuid === null
  )
  node.devices.allocated = own.filter(
    (device) => !device.allocatable && device.health === "healthy"
  ).length
}
for (const pool of pools) {
  const poolNodes = new Set(
    nodes.filter((node) => node.pool === pool.id).map((node) => node.id)
  )
  pool.membership.devices_allocatable = devices.filter(
    (device) =>
      device.mig_parent_device_uuid === null &&
      poolNodes.has(device.node) &&
      device.allocatable
  ).length
}

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

/**
 * One deployment's timeline, so the Activity tab has something to show offline.
 *
 * Written as the awkward case rather than the happy one: weights that took a
 * while, a rollout that filled in one replica at a time. A fixture where
 * everything worked immediately would not exercise the thing the tab exists to
 * make visible.
 */
const deploymentTimeline: PlaneEvent[] = [
  {
    id: uid("evt_"),
    cluster_id: clusters[0].id,
    kind: "deployment.created",
    severity: "info",
    subject: { type: "deployment", id: deployments[0].id },
    operation_id: null,
    message: "llama-70b-prod was created on iad-1 from recipe llama-70b",
    details: {
      deployment_id: deployments[0].id,
      name: "llama-70b-prod",
      cluster: "iad-1",
      desired_replicas: "4",
    },
    occurred_at: iso(5400),
    recorded_at: iso(5400),
  },
  {
    id: uid("evt_"),
    cluster_id: clusters[0].id,
    kind: "deployment.dispatched",
    severity: "info",
    subject: { type: "deployment", id: deployments[0].id },
    operation_id: null,
    message: "llama-70b-prod was sent to iad-1",
    details: {
      deployment_id: deployments[0].id,
      name: "llama-70b-prod",
      frames: "4",
    },
    occurred_at: iso(5399),
    recorded_at: iso(5399),
  },
  {
    id: uid("evt_"),
    cluster_id: clusters[0].id,
    kind: "deployment.pending",
    severity: "info",
    subject: { type: "deployment", id: deployments[0].id },
    operation_id: null,
    message: "reconciling revision 3",
    details: {
      deployment_id: deployments[0].id,
      name: "llama-70b-prod",
      reason: "Accepted",
      ready_replicas: "0",
      desired_replicas: "4",
    },
    occurred_at: iso(5392),
    recorded_at: iso(5392),
  },
  {
    id: uid("evt_"),
    cluster_id: clusters[0].id,
    kind: "deployment.pending",
    severity: "info",
    subject: { type: "deployment", id: deployments[0].id },
    operation_id: null,
    message: "waiting for art_01j9x2eeeeeeeeeeeeeeeeeeee to finish downloading",
    details: {
      deployment_id: deployments[0].id,
      name: "llama-70b-prod",
      reason: "WeightsNotResident",
      ready_replicas: "0",
      desired_replicas: "4",
    },
    occurred_at: iso(5380),
    recorded_at: iso(5380),
  },
  {
    id: uid("evt_"),
    cluster_id: clusters[0].id,
    kind: "deployment.pending",
    severity: "info",
    subject: { type: "deployment", id: deployments[0].id },
    operation_id: null,
    message: "building dep_llama-70b-prod from llama-70b",
    details: {
      deployment_id: deployments[0].id,
      name: "llama-70b-prod",
      reason: "Rendering",
      ready_replicas: "0",
      desired_replicas: "4",
    },
    occurred_at: iso(2760),
    recorded_at: iso(2760),
  },
  {
    id: uid("evt_"),
    cluster_id: clusters[0].id,
    kind: "deployment.placed",
    severity: "info",
    subject: { type: "deployment", id: deployments[0].id },
    operation_id: null,
    message: "0 of 4 replicas ready",
    details: {
      deployment_id: deployments[0].id,
      name: "llama-70b-prod",
      reason: "Applied",
      ready_replicas: "0",
      desired_replicas: "4",
    },
    occurred_at: iso(2700),
    recorded_at: iso(2700),
  },
  {
    id: uid("evt_"),
    cluster_id: clusters[0].id,
    kind: "deployment.running",
    severity: "info",
    subject: { type: "deployment", id: deployments[0].id },
    operation_id: null,
    message: "4 of 4 replicas ready",
    details: {
      deployment_id: deployments[0].id,
      name: "llama-70b-prod",
      reason: "Serving",
      ready_replicas: "4",
      desired_replicas: "4",
    },
    occurred_at: iso(1800),
    recorded_at: iso(1800),
  },
]

export const events: PlaneEvent[] = [
  ...deploymentTimeline,
  {
    id: uid("evt_"),
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
    id: uid("evt_"),
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
    id: uid("evt_"),
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
    id: uid("evt_"),
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
    id: uid("evt_"),
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
    id: uid("evt_"),
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

/**
 * What the clusters were already running before we arrived. One of each decision,
 * because the page's whole job is telling them apart: a fleet where everything
 * has been triaged looks the same as one where nothing has.
 */
export const discoveredWorkloads: DiscoveredWorkload[] = [
  {
    id: uid("dsc_"),
    cluster_id: clusters[0].id,
    cluster_name: clusters[0].name,
    namespace: "serving",
    name: "mixtral-8x7b",
    kind: "Deployment",
    model: "mistralai/Mixtral-8x7B-Instruct-v0.1",
    images: ["vllm/vllm-openai:v0.6.3"],
    replicas_desired: 2,
    replicas_ready: 2,
    devices_per_replica: 4,
    devices_total: 8,
    node_ids: [],
    labels: { app: "mixtral" },
    status: "new",
    deployment_id: null,
    spec_fingerprint: "sha256:1f4c9a2e",
    spec: {
      image: "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1",
      command: ["python3", "-m", "dynamo.vllm"],
      args: [
        "--model",
        "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "--served-model-name",
        "mixtral-8x7b",
        "--tensor-parallel-size",
        "4",
        "--max-model-len",
        "32768",
        "--gpu-memory-utilization",
        "0.90",
      ],
      env_names: ["HF_HOME"],
      requests: { cpu: "16", memory: "96Gi" },
      limits: {},
      node_selector: {},
    },
    project_id: null,
    note: null,
    decided_by: null,
    decided_at: null,
    first_seen_at: iso(180),
    observed_at: iso(2),
  },
  {
    id: uid("dsc_"),
    cluster_id: clusters[1].id,
    cluster_name: clusters[1].name,
    namespace: "research",
    name: "sd-xl-batch",
    kind: "StatefulSet",
    model: null,
    images: ["ghcr.io/acme/sdxl-worker:2024.11"],
    replicas_desired: 1,
    replicas_ready: 1,
    devices_per_replica: 2,
    devices_total: 2,
    node_ids: [],
    labels: { team: "research" },
    status: "tracked",
    deployment_id: null,
    spec_fingerprint: "sha256:7b0d31ca",
    spec: {
      image: "ghcr.io/acme/sdxl-worker:2024.11",
      command: [],
      args: [],
      env_names: ["HF_HOME"],
      requests: { cpu: "8", memory: "32Gi" },
      limits: {},
      node_selector: {},
    },
    project_id: projects[0].id,
    note: "owned by research, leave alone until Q3",
    decided_by: "operator",
    decided_at: iso(90),
    first_seen_at: iso(400),
    observed_at: iso(2),
  },
  {
    id: uid("dsc_"),
    cluster_id: clusters[0].id,
    cluster_name: clusters[0].name,
    namespace: "kube-system",
    name: "nvidia-device-plugin",
    kind: "DaemonSet",
    model: null,
    images: ["nvcr.io/nvidia/k8s-device-plugin:v0.17.0"],
    replicas_desired: 6,
    replicas_ready: 6,
    devices_per_replica: 0,
    devices_total: 0,
    node_ids: [],
    labels: {},
    status: "ignored",
    deployment_id: null,
    spec_fingerprint: "sha256:c02e8844",
    spec: {
      image: "nvcr.io/nvidia/k8s-device-plugin:v0.17.0",
      command: [],
      args: [],
      env_names: ["HF_HOME"],
      requests: {},
      limits: {},
      node_selector: {},
    },
    project_id: null,
    note: "infrastructure",
    decided_by: "operator",
    decided_at: iso(300),
    first_seen_at: iso(420),
    observed_at: iso(2),
  },
  {
    id: uid("dsc_"),
    cluster_id: clusters[1].id,
    cluster_name: clusters[1].name,
    namespace: "serving",
    name: "bge-embed",
    kind: "Deployment",
    model: "BAAI/bge-large-en-v1.5",
    images: ["vllm/vllm-openai:v0.6.3"],
    replicas_desired: 2,
    replicas_ready: 2,
    devices_per_replica: 1,
    devices_total: 2,
    node_ids: [],
    labels: { app: "bge" },
    status: "promoted",
    deployment_id: deployments[2].id,
    spec_fingerprint: "sha256:9ae10bd7",
    spec: {
      image: "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1",
      command: ["python3", "-m", "dynamo.vllm"],
      args: [
        "--model",
        "BAAI/bge-large-en-v1.5",
        "--served-model-name",
        "bge-embed",
        "--tensor-parallel-size",
        "1",
        "--max-model-len",
        "512",
      ],
      env_names: ["HF_HOME"],
      requests: { cpu: "4", memory: "16Gi" },
      limits: {},
      node_selector: {},
    },
    project_id: projects[0].id,
    note: null,
    decided_by: "operator",
    decided_at: iso(45),
    first_seen_at: iso(410),
    observed_at: iso(2),
  },
]

/**
 * Two runs against the same deployment at different concurrencies, plus one
 * that failed. Deliberately shaped like AIPerf's export rather than like
 * anything this console defines: the summary is stored verbatim, and a fixture
 * that used a tidy invented schema would make the generic renderer look better
 * than it is against a real harness.
 */
export const benchmarkRuns: BenchmarkRun[] = [
  {
    id: uid("bmk_"),
    request_id: null,
    deployment_id: deployments[0].id,
    cluster_id: clusters[0].id,
    endpoint_id: null,
    operation_id: null,
    harness: "aiperf",
    image: "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0",
    harness_version: "0.4.1",
    parameters: {
      concurrency: 64,
      request_count: 500,
      input_tokens_mean: 2000,
      input_tokens_stddev: 0,
      output_tokens_mean: 256,
      output_tokens_stddev: 0,
      endpoint_type: "chat",
      streaming: true,
      warmup_request_count: 10,
      extra: "",
    },
    step_index: 0,
    vantage: null,
    discard: false,
    requested_by: "operator",
    reason: "concurrency sweep, step 3",
    provenance: {
      model: "llama-70b",
      deployment_generation: 7,
      recipe_id: recipes[0].id,
      recipe_version: 3,
      engine_args: { "--max-num-seqs": 256, "--kv-cache-dtype": "fp8" },
      serving: {},
    },
    phase: "succeeded",
    summary: {
      time_to_first_token: {
        unit: "ms",
        avg: 412.7,
        min: 188.4,
        max: 1904.2,
        p50: 388.1,
        p90: 671.5,
        p95: 812.9,
        p99: 1402.6,
        std: 171.2,
      },
      inter_token_latency: {
        unit: "ms",
        avg: 18.4,
        min: 11.2,
        max: 74.9,
        p50: 17.6,
        p90: 24.1,
        p95: 28.8,
        p99: 41.3,
        std: 5.9,
      },
      request_latency: {
        unit: "ms",
        avg: 5124.3,
        min: 3011.7,
        max: 12408.5,
        p50: 4902.8,
        p90: 6511.2,
        p95: 7288.4,
        p99: 9914.7,
        std: 1088.6,
      },
      output_token_throughput: { unit: "tokens/sec", avg: 3208.1 },
      request_throughput: { unit: "requests/sec", avg: 12.4 },
      request_count: 500,
      error_count: 0,
    },
    artifacts_uri: null,
    error: null,
    created_at: iso(240),
    updated_at: iso(232),
    started_at: iso(239),
    finished_at: iso(232),
  },
  {
    id: uid("bmk_"),
    request_id: null,
    deployment_id: deployments[0].id,
    cluster_id: clusters[0].id,
    endpoint_id: null,
    operation_id: null,
    harness: "aiperf",
    image: "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0",
    harness_version: "0.4.1",
    parameters: {
      concurrency: 16,
      request_count: 500,
      input_tokens_mean: 2000,
      input_tokens_stddev: 0,
      output_tokens_mean: 256,
      output_tokens_stddev: 0,
      endpoint_type: "chat",
      streaming: true,
      warmup_request_count: 10,
      extra: "",
    },
    step_index: 0,
    vantage: null,
    discard: false,
    requested_by: "operator",
    reason: "concurrency sweep, step 1",
    provenance: {
      model: "llama-70b",
      deployment_generation: 7,
      recipe_id: recipes[0].id,
      recipe_version: 3,
      engine_args: { "--max-num-seqs": 256, "--kv-cache-dtype": "fp8" },
      serving: {},
    },
    phase: "succeeded",
    summary: {
      time_to_first_token: {
        unit: "ms",
        avg: 174.2,
        min: 92.8,
        max: 611.4,
        p50: 166.9,
        p90: 248.3,
        p95: 291.7,
        p99: 448.1,
        std: 58.4,
      },
      inter_token_latency: {
        unit: "ms",
        avg: 11.1,
        min: 8.4,
        max: 38.2,
        p50: 10.8,
        p90: 13.9,
        p95: 15.4,
        p99: 21.7,
        std: 2.8,
      },
      request_latency: {
        unit: "ms",
        avg: 3018.9,
        min: 2288.1,
        max: 6104.7,
        p50: 2954.2,
        p90: 3611.8,
        p95: 3944.5,
        p99: 5122.3,
        std: 442.1,
      },
      output_token_throughput: { unit: "tokens/sec", avg: 1412.6 },
      request_throughput: { unit: "requests/sec", avg: 5.3 },
      request_count: 500,
      error_count: 0,
    },
    artifacts_uri: null,
    error: null,
    created_at: iso(300),
    updated_at: iso(294),
    started_at: iso(299),
    finished_at: iso(294),
  },
  {
    id: uid("bmk_"),
    request_id: null,
    deployment_id: deployments[2].id,
    cluster_id: deployments[2].cluster_id,
    endpoint_id: null,
    operation_id: null,
    harness: "aiperf",
    image: "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0",
    harness_version: null,
    parameters: {
      concurrency: 128,
      request_count: 1000,
      input_tokens_mean: 8000,
      input_tokens_stddev: 0,
      output_tokens_mean: 512,
      output_tokens_stddev: 0,
      endpoint_type: "chat",
      streaming: true,
      warmup_request_count: 10,
      extra: "",
    },
    step_index: 0,
    vantage: null,
    discard: false,
    requested_by: "operator",
    reason: "long-context saturation",
    provenance: {
      model: "mixtral-8x22b",
      deployment_generation: 2,
      recipe_id: recipes[1].id,
      recipe_version: 1,
      engine_args: {},
      serving: {},
    },
    phase: "failed",
    summary: null,
    artifacts_uri: null,
    error: "the benchmark job hit its deadline before reporting a result",
    created_at: iso(90),
    updated_at: iso(58),
    started_at: iso(89),
    finished_at: iso(58),
  },
]
