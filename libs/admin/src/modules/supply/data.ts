import type {
  Cluster,
  ClusterCapacity,
  ClusterComponent,
  ClusterDetail,
  ClusterSync,
} from "@/modules/clusters/types"
import type {
  Device,
  DeviceHealth,
  Pool,
  ProvisioningSource,
  SupplyNode,
  TopologyDomain,
  TopologyKind,
} from "./types"

const now = Date.now()

const iso = (minutesAgo: number) =>
  new Date(now - minutesAgo * 60_000).toISOString()

const DEPLOYMENTS = [
  {
    id: "dep_llama70b_fp8",
    name: "llama-3.3-70b-fp8",
    project_id: "prj_core",
    project_name: "core-serving",
  },
  {
    id: "dep_mixtral_bf16",
    name: "mixtral-8x7b-bf16",
    project_id: "prj_research",
    project_name: "research",
  },
  {
    id: "dep_embed_bge",
    name: "bge-m3-embed",
    project_id: "prj_platform",
    project_name: "platform",
  },
]

const DEVICE_MEMORY_GB: Record<string, number> = {
  H100_SXM_80GB: 80,
  A100_SXM_80GB: 80,
  L40S_48GB: 48,
}

interface DeviceFault {
  node: number
  device: number
  health: DeviceHealth
  throttled?: boolean
  mig_profile?: string
}

interface PoolSpec {
  id: string
  name: string
  device_class: string
  node_shape: string
  devices_per_node: number
  min_nodes: number
  max_nodes: number
  provisioning_source: ProvisioningSource
  labels: Record<string, string>
  taints: string[]
  topology_kind: TopologyKind
  interconnect: string
  bandwidth_gbps: number | null
  cpu_cores: number
  memory_gb: number
  driver_version: string
  nodes: number
  not_ready: number[]
  cordoned: number[]
  faults: DeviceFault[]
  allocated_nodes: number
  last_scaled_at: string | null
  created_at: string
}

interface ClusterSpec {
  id: string
  name: string
  display_name: string
  region: string
  phase: ClusterDetail["phase"]
  reachable: boolean
  labels: Record<string, string>
  sync: ClusterSync
  components: ClusterComponent[]
  pools: PoolSpec[]
  enrolled_at: string
}

const CLUSTER_SPECS: ClusterSpec[] = [
  {
    id: "clu_iad1_a",
    name: "iad1-a",
    display_name: "Ashburn A",
    region: "us-east-1",
    phase: "active",
    reachable: true,
    labels: { fabric: "nvswitch", tier: "production" },
    sync: {
      state: "connected",
      operator_version: "0.4.2",
      connected_since: iso(6_240),
      last_heartbeat_at: iso(0.1),
      last_sync_at: iso(0.3),
      stream_lag_ms: 42,
      pending_declarations: 0,
      last_error: null,
    },
    components: [
      {
        name: "operator",
        kind: "operator",
        declared_version: "0.4.2",
        observed_version: "0.4.2",
        phase: "healthy",
        last_updated_at: iso(4_320),
      },
      {
        name: "vllm-runtime",
        kind: "runtime",
        declared_version: "0.9.1",
        observed_version: "0.9.1",
        phase: "healthy",
        last_updated_at: iso(1_440),
      },
      {
        name: "weight-cache",
        kind: "cache",
        declared_version: "0.2.0",
        observed_version: "0.2.0",
        phase: "healthy",
        last_updated_at: iso(10_080),
      },
      {
        name: "dcgm-exporter",
        kind: "exporter",
        declared_version: "3.3.5",
        observed_version: "3.3.5",
        phase: "healthy",
        last_updated_at: iso(20_160),
      },
    ],
    enrolled_at: iso(129_600),
    pools: [
      {
        id: "pol_iad1_a_h100",
        name: "h100-sxm",
        device_class: "H100_SXM_80GB",
        node_shape: "HGX-H100-8G",
        devices_per_node: 8,
        min_nodes: 8,
        max_nodes: 16,
        provisioning_source: "owned_bare_metal",
        labels: { tier: "production", fabric: "nvswitch" },
        taints: ["hangar.io/dedicated=inference:NoSchedule"],
        topology_kind: "nvswitch",
        interconnect: "NVSwitch (NVLink 4.0)",
        bandwidth_gbps: 900,
        cpu_cores: 192,
        memory_gb: 2048,
        driver_version: "560.35.03",
        nodes: 12,
        not_ready: [],
        cordoned: [10],
        faults: [{ node: 7, device: 3, health: "degraded", throttled: true }],
        allocated_nodes: 9,
        last_scaled_at: iso(3_600),
        created_at: iso(129_600),
      },
    ],
  },
  {
    id: "clu_sjc2_b",
    name: "sjc2-b",
    display_name: "San Jose B",
    region: "us-west-2",
    phase: "active",
    reachable: false,
    labels: { fabric: "nvswitch", tier: "production" },
    sync: {
      state: "reconnecting",
      operator_version: "0.4.1",
      connected_since: null,
      last_heartbeat_at: iso(4),
      last_sync_at: iso(4),
      stream_lag_ms: null,
      pending_declarations: 3,
      last_error: "stream closed by peer: rst_stream (code 2)",
    },
    components: [
      {
        name: "operator",
        kind: "operator",
        declared_version: "0.4.2",
        observed_version: "0.4.1",
        phase: "upgrading",
        last_updated_at: iso(60),
      },
      {
        name: "vllm-runtime",
        kind: "runtime",
        declared_version: "0.9.1",
        observed_version: "0.9.0",
        phase: "degraded",
        last_updated_at: iso(2_880),
      },
      {
        name: "otel-collector",
        kind: "collector",
        declared_version: "0.106.0",
        observed_version: null,
        phase: "missing",
        last_updated_at: null,
      },
    ],
    enrolled_at: iso(86_400),
    pools: [
      {
        id: "pol_sjc2_b_h100",
        name: "h100-sxm",
        device_class: "H100_SXM_80GB",
        node_shape: "HGX-H100-8G",
        devices_per_node: 8,
        min_nodes: 4,
        max_nodes: 12,
        provisioning_source: "colocation",
        labels: { tier: "production", fabric: "nvswitch" },
        taints: [],
        topology_kind: "nvswitch",
        interconnect: "NVSwitch (NVLink 4.0)",
        bandwidth_gbps: 900,
        cpu_cores: 192,
        memory_gb: 2048,
        driver_version: "560.35.03",
        nodes: 6,
        not_ready: [5],
        cordoned: [4, 5],
        faults: [
          { node: 1, device: 0, health: "failed" },
          { node: 2, device: 6, health: "degraded", throttled: true },
          { node: 5, device: 2, health: "unknown" },
        ],
        allocated_nodes: 4,
        last_scaled_at: iso(14_400),
        created_at: iso(86_400),
      },
      {
        id: "pol_sjc2_b_a100",
        name: "a100-legacy",
        device_class: "A100_SXM_80GB",
        node_shape: "HGX-A100-8G",
        devices_per_node: 8,
        min_nodes: 2,
        max_nodes: 4,
        provisioning_source: "colocation",
        labels: { tier: "batch" },
        taints: ["hangar.io/legacy=true:PreferNoSchedule"],
        topology_kind: "nvlink",
        interconnect: "NVLink 3.0",
        bandwidth_gbps: 600,
        cpu_cores: 128,
        memory_gb: 1024,
        driver_version: "550.90.07",
        nodes: 2,
        not_ready: [],
        cordoned: [],
        faults: [
          {
            node: 0,
            device: 0,
            health: "healthy",
            mig_profile: "1g.10gb",
          },
        ],
        allocated_nodes: 1,
        last_scaled_at: iso(43_200),
        created_at: iso(86_400),
      },
    ],
  },
  {
    id: "clu_ams1_a",
    name: "ams1-a",
    display_name: "Amsterdam A",
    region: "eu-west-1",
    phase: "active",
    reachable: true,
    labels: { fabric: "pcie", tier: "production", residency: "eu" },
    sync: {
      state: "connected",
      operator_version: "0.4.2",
      connected_since: iso(2_880),
      last_heartbeat_at: iso(0.2),
      last_sync_at: iso(1),
      stream_lag_ms: 88,
      pending_declarations: 0,
      last_error: null,
    },
    components: [
      {
        name: "operator",
        kind: "operator",
        declared_version: "0.4.2",
        observed_version: "0.4.2",
        phase: "healthy",
        last_updated_at: iso(4_320),
      },
      {
        name: "gateway",
        kind: "gateway",
        declared_version: "1.2.0",
        observed_version: "1.2.0",
        phase: "healthy",
        last_updated_at: iso(7_200),
      },
    ],
    enrolled_at: iso(43_200),
    pools: [
      {
        id: "pol_ams1_a_l40s",
        name: "l40s-pcie",
        device_class: "L40S_48GB",
        node_shape: "PCIe-L40S-8G",
        devices_per_node: 8,
        min_nodes: 4,
        max_nodes: 10,
        provisioning_source: "cloud_reserved",
        labels: { residency: "eu", tier: "production" },
        taints: [],
        topology_kind: "pcie",
        interconnect: "PCIe Gen5 x16",
        bandwidth_gbps: 64,
        cpu_cores: 96,
        memory_gb: 768,
        driver_version: "560.35.03",
        nodes: 6,
        not_ready: [],
        cordoned: [],
        faults: [],
        allocated_nodes: 3,
        last_scaled_at: iso(1_440),
        created_at: iso(43_200),
      },
    ],
  },
  {
    id: "clu_fra1_c",
    name: "fra1-c",
    display_name: "Frankfurt C",
    region: "eu-central-1",
    phase: "pending",
    reachable: false,
    labels: { tier: "staging", residency: "eu" },
    sync: {
      state: "disconnected",
      operator_version: "0.4.2",
      connected_since: null,
      last_heartbeat_at: null,
      last_sync_at: null,
      stream_lag_ms: null,
      pending_declarations: 1,
      last_error: "operator has not yet dialled in",
    },
    components: [],
    enrolled_at: iso(45),
    pools: [
      {
        id: "pol_fra1_c_h200",
        name: "h200-sxm",
        device_class: "H200_SXM_141GB",
        node_shape: "HGX-H200-8G",
        devices_per_node: 8,
        min_nodes: 2,
        max_nodes: 8,
        provisioning_source: "cloud_on_demand",
        labels: { residency: "eu", tier: "staging" },
        taints: [],
        topology_kind: "nvswitch",
        interconnect: "NVSwitch (NVLink 5.0)",
        bandwidth_gbps: 900,
        cpu_cores: 224,
        memory_gb: 2048,
        driver_version: "560.35.03",
        nodes: 0,
        not_ready: [],
        cordoned: [],
        faults: [],
        allocated_nodes: 0,
        last_scaled_at: null,
        created_at: iso(45),
      },
    ],
  },
]

const nodes: SupplyNode[] = []
const devices: Device[] = []
const domains: TopologyDomain[] = []
const pools: Pool[] = []
const clusters: ClusterDetail[] = []

let allocationSeed = 0

function buildPool(spec: ClusterSpec, pool: PoolSpec): Pool {
  const slug = pool.id.slice(4)
  const membership = {
    nodes_total: 0,
    nodes_ready: 0,
    nodes_cordoned: 0,
    devices_total: 0,
    devices_healthy: 0,
    devices_allocated: 0,
    topology_domains: 0,
    last_scaled_at: pool.last_scaled_at,
  }

  for (let n = 0; n < pool.nodes; n++) {
    const nodeId = `nod_${slug}_${String(n + 1).padStart(3, "0")}`
    const nodeName = `${spec.name}-${pool.name}-${String(n + 1).padStart(3, "0")}`
    const condition = pool.not_ready.includes(n) ? "not_ready" : "ready"
    const cordoned = pool.cordoned.includes(n)
    const domainId = `tpd_${slug}_${String(n + 1).padStart(3, "0")}`
    const allocateNode = n < pool.allocated_nodes && condition === "ready"

    let healthyOnNode = 0
    let allocatedOnNode = 0

    for (let d = 0; d < pool.devices_per_node; d++) {
      const fault = pool.faults.find((f) => f.node === n && f.device === d)
      const health: DeviceHealth =
        condition === "not_ready" ? "unknown" : (fault?.health ?? "healthy")
      if (health === "healthy") healthyOnNode++

      const allocate = allocateNode && health === "healthy" && !cordoned
      if (allocate) allocatedOnNode++
      const deployment = DEPLOYMENTS[allocationSeed++ % DEPLOYMENTS.length]

      devices.push({
        id: `dev_${slug}_${String(n + 1).padStart(3, "0")}_${d}`,
        cluster_id: spec.id,
        cluster_name: spec.name,
        node_id: nodeId,
        node_name: nodeName,
        index: d,
        uuid: `GPU-${slug.replace(/_/g, "")}-${String(n + 1).padStart(3, "0")}-${String(d).padStart(2, "0")}`,
        device_class: pool.device_class,
        memory_gb: DEVICE_MEMORY_GB[pool.device_class] ?? 141,
        memory_used_gb: allocate
          ? Math.round((DEVICE_MEMORY_GB[pool.device_class] ?? 141) * 0.78)
          : 0,
        health,
        throttled: fault?.throttled ?? false,
        throttle_reasons: fault?.throttled ? ["sw_thermal_slowdown"] : [],
        ecc_errors: {
          volatile: health === "healthy" ? 0 : 4,
          aggregate: health === "healthy" ? 0 : 137,
        },
        mig_profile: fault?.mig_profile ?? null,
        topology_domain_id: domainId,
        topology_kind: pool.topology_kind,
        allocation: allocate
          ? {
              replica_id: `rep_${deployment.id.slice(4)}_${String(allocationSeed).padStart(4, "0")}`,
              deployment_id: deployment.id,
              deployment_name: deployment.name,
              project_id: deployment.project_id,
              project_name: deployment.project_name,
              allocated_at: iso(120 + allocationSeed * 7),
            }
          : null,
        last_seen_at: condition === "ready" ? iso(0.2) : iso(35),
      })
    }

    domains.push({
      id: domainId,
      cluster_id: spec.id,
      cluster_name: spec.name,
      node_id: nodeId,
      node_name: nodeName,
      kind: pool.topology_kind,
      interconnect: pool.interconnect,
      bandwidth_gbps: pool.bandwidth_gbps,
      device_count: pool.devices_per_node,
      devices_healthy: healthyOnNode,
      devices_allocated: allocatedOnNode,
    })

    nodes.push({
      id: nodeId,
      cluster_id: spec.id,
      cluster_name: spec.name,
      name: nodeName,
      pool_id: pool.id,
      pool_name: pool.name,
      condition,
      cordoned,
      instance_type: pool.node_shape,
      kernel_version: "6.8.0-45-generic",
      driver_version: pool.driver_version,
      cpu_cores: pool.cpu_cores,
      memory_gb: pool.memory_gb,
      device_class: pool.device_class,
      device_count: pool.devices_per_node,
      devices_healthy: healthyOnNode,
      devices_allocated: allocatedOnNode,
      topology_domain_ids: [domainId],
      labels: pool.labels,
      taints: pool.taints,
      joined_at: iso(40_000 - n * 120),
      last_seen_at: condition === "ready" ? iso(0.2) : iso(35),
    })

    membership.nodes_total++
    if (condition === "ready") membership.nodes_ready++
    if (cordoned) membership.nodes_cordoned++
    membership.devices_total += pool.devices_per_node
    membership.devices_healthy += healthyOnNode
    membership.devices_allocated += allocatedOnNode
    membership.topology_domains++
  }

  return {
    id: pool.id,
    name: pool.name,
    cluster_id: spec.id,
    cluster_name: spec.name,
    declared: {
      device_class: pool.device_class,
      node_shape: pool.node_shape,
      devices_per_node: pool.devices_per_node,
      min_nodes: pool.min_nodes,
      max_nodes: pool.max_nodes,
      provisioning_source: pool.provisioning_source,
      labels: pool.labels,
      taints: pool.taints,
    },
    observed: membership,
    created_at: pool.created_at,
    updated_at: pool.last_scaled_at,
  }
}

for (const spec of CLUSTER_SPECS) {
  const capacity: ClusterCapacity = {
    nodes_total: 0,
    nodes_ready: 0,
    devices_total: 0,
    devices_healthy: 0,
    devices_allocated: 0,
  }

  const clusterPools = spec.pools.map((pool) => buildPool(spec, pool))
  for (const pool of clusterPools) {
    capacity.nodes_total += pool.observed.nodes_total
    capacity.nodes_ready += pool.observed.nodes_ready
    capacity.devices_total += pool.observed.devices_total
    capacity.devices_healthy += pool.observed.devices_healthy
    capacity.devices_allocated += pool.observed.devices_allocated
  }
  pools.push(...clusterPools)

  clusters.push({
    id: spec.id,
    name: spec.name,
    display_name: spec.display_name,
    region: spec.region,
    phase: spec.phase,
    reachable: spec.reachable,
    labels: spec.labels,
    device_classes: [
      ...new Set(
        clusterPools
          .filter((p) => p.observed.devices_total > 0)
          .map((p) => p.declared.device_class)
      ),
    ],
    capacity,
    sync: spec.sync,
    enrolled_at: spec.enrolled_at,
    created_at: spec.enrolled_at,
    updated_at: spec.sync.last_sync_at,
    topology_domain_count: clusterPools.reduce(
      (acc, p) => acc + p.observed.topology_domains,
      0
    ),
    components: spec.components,
  })
}

export function clusterSummary(cluster: ClusterDetail): Cluster {
  const { topology_domain_count, components, ...rest } = cluster
  void topology_domain_count
  void components
  return rest
}

export const db = { clusters, pools, nodes, devices, domains }
