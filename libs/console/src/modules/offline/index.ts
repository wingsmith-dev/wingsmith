import {
  page,
  registerFixtures,
  type FixtureContext,
  type FixtureQuery,
} from "@/lib/api/fixtures"
import { ConflictError, NotFoundError } from "@/lib/api/transport"
import { fitment } from "./fitment"
import {
  OPERATION_TYPES,
  isOpen,
  type Operation,
  type OperationCreateRequest,
} from "@/modules/machinery/types"
import {
  allocations,
  artifacts,
  benchmarkRuns,
  clusters,
  components,
  deployments,
  deviceClasses,
  devices,
  discoveredWorkloads,
  domains,
  endpoints,
  events,
  nodes,
  operations,
  policies,
  pools,
  priorityClasses,
  projects,
  tenants,
  quotas,
  workloads,
} from "./data"
import { revision, uid } from "./ids"
import { collection } from "./registry"
import type { Artifact, Deployment, Workload } from "@/modules/demand/types"
import {
  emptyCapabilities,
  emptyServing,
  emptyUpstream,
} from "@/modules/demand/display"
import type { PlaneConfig } from "@/modules/system/types"
import type {
  BenchmarkCreateRequest,
  BenchmarkHarness,
  BenchmarkRun,
} from "@/modules/benchmarks/types"
import type {
  DecisionRequest,
  DiscoveredWorkload,
  PromoteRequest,
  ServingDraft,
} from "@/modules/discovered/types"

const bool = (value: boolean | undefined, actual: boolean) =>
  value === undefined || value === actual

const eq = (want: string | undefined, ...actual: (string | null)[]) =>
  want === undefined || actual.includes(want)

/**
 * The batch rollup, counted out of the fixture supply rather than stored beside
 * it, so a card cannot disagree with the tables it links to. Registered ahead of
 * the cluster collection because `/clusters/:ref` would otherwise swallow
 * `/clusters/rollups` — the same route-order trap the plane has.
 */
function clusterRollup(clusterId: string) {
  const own = devices.filter((d) => d.cluster === clusterId)
  const free = own.filter((d) => d.allocatable)
  const ownNodes = nodes.filter((n) => n.cluster === clusterId)
  const byNode = new Map<string, { total: number; free: number }>()
  for (const device of own) {
    const seen = byNode.get(device.node) ?? { total: 0, free: 0 }
    byNode.set(device.node, {
      total: seen.total + 1,
      free: seen.free + (device.allocatable ? 1 : 0),
    })
  }
  const domains = new Map<string, number>()
  for (const device of free) {
    if (!device.topology?.id) continue
    domains.set(device.topology.id, (domains.get(device.topology.id) ?? 0) + 1)
  }
  const block = Math.max(0, ...domains.values())

  return {
    cluster_id: clusterId,
    nodes_total: ownNodes.length,
    nodes_ready: ownNodes.filter((n) => n.ready).length,
    devices_total: own.length,
    devices_allocatable: free.length,
    devices_allocated: own.length - free.length,
    pool_count: pools.filter((p) => p.cluster_id === clusterId).length,
    deployment_count: deployments.filter((d) => d.cluster_id === clusterId)
      .length,
    headroom: {
      free_devices: free.length,
      whole_nodes_free: [...byNode.values()].filter(
        (n) => n.total > 0 && n.total === n.free
      ).length,
      largest_contiguous_block: block === 0 && free.length > 0 ? 1 : block,
    },
  }
}

registerFixtures([
  {
    method: "GET",
    path: "/clusters/rollups",
    handler: ({ query }: FixtureContext) =>
      page(
        clusters.map((c) => clusterRollup(c.id)),
        query
      ),
  },
  {
    method: "GET",
    path: "/clusters/:ref/rollup",
    handler: ({ params }: FixtureContext) => {
      const id = clusterRef(params.ref)
      if (!id) throw new NotFoundError(`no cluster '${params.ref}'`)
      return clusterRollup(id)
    },
  },
  {
    method: "GET",
    path: "/clusters/:ref/headroom",
    handler: ({ params }: FixtureContext) => {
      const id = clusterRef(params.ref)
      if (!id) throw new NotFoundError(`no cluster '${params.ref}'`)
      return clusterRollup(id).headroom
    },
  },
])

collection({
  path: "/clusters",
  singular: "cluster",
  rows: () => clusters,
  names: (c) => [c.name],
  searchable: (c) => [c.name, c.display_name, c.region],
  labels: (c) => c.labels,
  filter: (c, q) =>
    eq(q.get("region"), c.region) && eq(q.get("phase"), c.phase),
})

const clusterRef = (ref: string | undefined) =>
  clusters.find((c) => c.id === ref || c.name === ref)?.id

const poolRef = (ref: string | undefined) =>
  pools.find((p) => p.id === ref || p.name === ref)?.id

const nodeRef = (ref: string | undefined) =>
  nodes.find((n) => n.id === ref || n.name === ref)?.id

collection({
  path: "/nodes",
  singular: "node",
  rows: () => nodes,
  names: (n) => [n.name],
  searchable: (n) => [n.name, n.cluster_name, n.pool_name],
  labels: (n) => n.labels,
  filter: (n, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), n.cluster) &&
    eq(poolRef(q.get("pool")) ?? q.get("pool"), n.pool) &&
    eq(q.get("condition"), n.condition) &&
    bool(q.bool("ready"), n.ready) &&
    bool(q.bool("cordoned"), n.cordoned),
})

collection({
  path: "/devices",
  singular: "device",
  rows: () => devices,
  names: (d) => [d.device_uuid],
  filter: (d, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), d.cluster) &&
    eq(nodeRef(q.get("node")) ?? q.get("node"), d.node) &&
    eq(q.get("class"), d.device_class) &&
    eq(q.get("health"), d.health) &&
    eq(q.get("topology_domain"), d.topology?.id ?? null) &&
    bool(q.bool("allocatable"), d.allocatable),
})

/**
 * The catalog is keyed by name and is not paginated, so it is registered by
 * hand rather than through `collection` — which assumes an id and a cursor,
 * and a device class has neither.
 */
registerFixtures([
  {
    method: "GET",
    path: "/device-classes",
    handler: ({ query }: FixtureContext) => {
      const architecture = query.get("architecture")
      const present = query.bool("present")
      const cluster = clusterRef(query.get("cluster")) ?? query.get("cluster")

      const items = deviceClasses(cluster)
        .filter(
          (c) => architecture === undefined || c.architecture === architecture
        )
        .filter(
          (c) => present === undefined || c.inventory.devices > 0 === present
        )
      return { items, count: items.length, next_cursor: null }
    },
  },
  {
    method: "GET",
    path: "/device-classes/:name",
    handler: ({ params }: FixtureContext) => {
      const found = deviceClasses().find((c) => c.name === params.name)
      if (!found) throw new NotFoundError(`no device class '${params.name}'`)
      return found
    },
  },
])

collection({
  path: "/topology-domains",
  singular: "topology domain",
  rows: () => domains,
  names: (t) => [t.identifier],
  filter: (t, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), t.cluster) &&
    eq(nodeRef(q.get("node")) ?? q.get("node"), ...t.nodes) &&
    eq(q.get("kind"), t.kind) &&
    bool(q.bool("spans_nodes"), t.spans_nodes),
})

collection({
  path: "/pools",
  singular: "pool",
  rows: () => pools,
  names: (p) => [p.name],
  searchable: (p) => [p.name, p.device_class],
  labels: (p) => p.labels,
  filter: (p, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), p.cluster_id) &&
    eq(q.get("class"), p.device_class) &&
    bool(q.bool("enabled"), p.enabled),
})

/**
 * The offline stand-in for the plane's HuggingFace resolve. It is registered
 * ahead of the artifact collection because the fixture router takes the first
 * matching route, and `/artifacts/:ref` would otherwise swallow `/resolve` the
 * same way the plane's route order would.
 *
 * The digest here is fabricated, not derived — there is no Hub offline. What it
 * does reproduce faithfully is the three answers a console has to render apart:
 * resolved, gated, and no such repo.
 */
const HF_FIXTURES: Record<
  string,
  {
    format: string
    size: number
    parameters: number
    licence: string
    /** Present only where a corpus has published a tested configuration. */
    upstream?: {
      tensor_parallel: number
      tool_call_parser: string | null
      verified_hardware: string[]
      /** Set only on a model upstream serves from a build of its own. */
      docker_image?: string
      nightly_required?: boolean
    }
  }
> = {
  "openai/gpt-oss-20b": {
    format: "safetensors",
    size: 41_231_686_042,
    parameters: 20_900_000_000,
    licence: "apache-2.0",
    upstream: {
      tensor_parallel: 1,
      tool_call_parser: "openai",
      verified_hardware: ["H100", "H200"],
    },
  },
  "Qwen/Qwen3-32B": {
    format: "safetensors",
    size: 65_527_486_976,
    parameters: 32_800_000_000,
    licence: "apache-2.0",
  },
  /**
   * The awkward case, kept here because it is the one the flow gets wrong quietly.
   * Upstream serves it from its own vLLM build, so the image the plane derives
   * loads nothing, and every other field on the page still looks answered.
   */
  "zai-org/GLM-5.3-Flash": {
    format: "safetensors",
    size: 321_000_000_000,
    parameters: 321_000_000_000,
    licence: "mit",
    upstream: {
      tensor_parallel: 8,
      tool_call_parser: "glm47",
      verified_hardware: ["H100", "B200", "GB200"],
      docker_image: "vllm/vllm-openai:glm53-flash",
      nightly_required: true,
    },
  },
}

const HF_GATED = ["meta-llama/Llama-3.1-70B-Instruct"]

function hfSlug(source: string): string {
  const value = source
    .trim()
    .replace(/^hf:\/\//i, "")
    .replace(/^hf:/i, "")
  const parts = value.split("/").filter(Boolean)
  return parts.length === 2 ? parts.join("/") : ""
}

/**
 * Registered ahead of the artifact collection for the same reason `/resolve` is:
 * the fixture router takes the first matching route, and `/artifacts/:ref` would
 * otherwise swallow the sub-resource.
 */
registerFixtures([
  {
    method: "GET",
    path: "/artifacts/:ref/fitment",
    handler: ({ params, query }: FixtureContext) => {
      const found = artifacts.find(
        (a) => a.id === params.ref || a.name === params.ref
      )
      if (!found) throw new NotFoundError(`no artifact '${params.ref}'`)
      return fitment(found, deviceClasses(), {
        scope: query.get("scope") ?? undefined,
        context_length: query.int("context_length"),
        concurrency: query.int("concurrency"),
        quantization: query.get("quantization") ?? undefined,
        max_devices: query.int("max_devices"),
      })
    },
  },
])

/**
 * What a re-read of a source would say now, per slug.
 *
 * Only the derived half, because that is all `POST /artifacts/:ref/refresh` writes.
 * `BAAI/bge-m3`'s entry is the interesting one: the offline `bge-m3-embed` carries no
 * shape, so refreshing it moves that artifact from a weights floor to a priced context
 * window — the whole reason the route exists, reproduced rather than described.
 */
const HF_REFRESH: Record<string, Record<string, unknown>> = {
  "BAAI/bge-m3": {
    source: "huggingface",
    huggingface_id: "BAAI/bge-m3",
    quantization: "fp16",
    dtype: "float32",
    architecture: "XLMRobertaModel",
    is_moe: false,
    active_parameter_count: null,
    context_length: 8192,
    shape: {
      num_hidden_layers: 24,
      num_key_value_heads: 16,
      head_dim: 64,
      kv_lora_rank: null,
      qk_rope_head_dim: null,
      context_length: 8192,
      num_attention_heads: 16,
      hidden_size: 1024,
    },
  },
}

/**
 * Registered ahead of the artifact collection for the same reason `/resolve` is.
 *
 * Mutates the fixture row, so a refresh is visible in the panel that prompted it
 * rather than being a message about a write that did not happen. The three refusals
 * are all reachable: an artifact with no source, a source the offline Hub does not
 * carry, and — for `llama-3.3-70b-instruct`, whose locations are `s3` — the "nowhere
 * to go back and ask" case a console has to render as a disabled button.
 */
registerFixtures([
  {
    method: "POST",
    path: "/artifacts/:ref/refresh",
    handler: ({ params }: FixtureContext) => {
      const found = artifacts.find(
        (a) => a.id === params.ref || a.name === params.ref
      )
      if (!found) throw new NotFoundError(`no artifact '${params.ref}'`)

      const source = artifactSource(found)
      if (!source)
        throw new ConflictError(
          "invalid",
          `artifact ${found.id} records no source to re-read; only huggingface artifacts can be refreshed`,
          {
            artifact: found.id,
            expected: "metadata.huggingface_id or an hf:// location",
          }
        )

      const slug = hfSlug(source)
      const fresh = HF_REFRESH[slug]
      if (!fresh) throw new NotFoundError(`no HuggingFace repo '${slug}'`)

      const changed: string[] = []
      const metadata: Record<string, unknown> = { ...found.metadata }
      for (const [key, value] of Object.entries(fresh)) {
        if (JSON.stringify(metadata[key]) === JSON.stringify(value)) continue
        metadata[key] = value
        changed.push(`metadata.${key}`)
      }
      found.metadata = metadata

      return {
        artifact: found,
        source,
        changed,
        notes: found.verified
          ? []
          : [
              {
                level: "warn",
                message:
                  "the weights are not content-addressable, so the digest names the commit rather than hashing the bytes",
              },
            ],
      }
    },
  },
])

/** `metadata.huggingface_id` first, then an `hf://` location with its revision dropped. */
function artifactSource(artifact: Artifact): string | null {
  const stored = artifact.metadata?.huggingface_id
  if (typeof stored === "string" && hfSlug(stored)) return `hf://${stored}`
  for (const location of artifact.locations) {
    const candidate = location.uri.split("@")[0]
    if (/^hf:/i.test(candidate) && hfSlug(candidate))
      return `hf://${hfSlug(candidate)}`
  }
  return null
}

registerFixtures([
  {
    method: "GET",
    path: "/artifacts/resolve",
    handler: ({ query }: FixtureContext) => {
      const source = query.get("source") ?? ""
      const slug = hfSlug(source)
      if (!slug)
        throw new ConflictError(
          "invalid",
          `'${source}' is not a HuggingFace repo — expected owner/name`
        )
      if (HF_GATED.includes(slug))
        throw new ConflictError(
          "invalid",
          `'${slug}' is gated on HuggingFace and the plane has no accepted licence for it`,
          { source, token: "HANGAR_HF_TOKEN" }
        )
      const found = HF_FIXTURES[slug]
      if (!found) throw new NotFoundError(`no HuggingFace repo '${slug}'`)

      const sha = commitOf(slug)
      const digest = `sha256:${sha.repeat(2).slice(0, 64)}`
      const uri = `hf://${slug}@${sha}`
      return {
        reference: `hf:${slug}@${sha}`,
        digest,
        name: slug.split("/")[1],
        kind: "weights",
        format: found.format,
        revision: sha,
        size_bytes: found.size,
        licence: found.licence,
        parameter_count: found.parameters,
        source_scheme: "hf",
        source_uri: uri,
        verified: true,
        metadata: {
          source: "huggingface",
          huggingface_id: slug,
          revision: sha,
          quantization: null,
          dtype: "bfloat16",
          architecture: null,
          is_moe: slug.includes("oss"),
          active_parameter_count: null,
          context_length: 131072,
        },
        locations: [{ scheme: "hf", uri, region: null, preferred: true }],
        serving: {
          device_count: 1,
          device_memory_mib_min: 81920,
          context_length: 131072,
          quantization: null,
          dtype: "bfloat16",
        },
        /**
         * A corpus entry for the demo, so the create flow can show what it looks
         * like when a value is somebody else's tested answer rather than ours.
         * Absent for repos nobody upstream has published a configuration for,
         * which is the ordinary case.
         */
        upstream: found.upstream
          ? {
              source: "vllm",
              source_uri: `https://github.com/vllm-project/recipes/blob/main/${slug}.md`,
              snapshot_uri: `https://github.com/vllm-project/recipes/blob/${sha}/${slug}.md`,
              model_id: slug,
              variant: "default",
              precision: null,
              engine: "vllm",
              engine_version_min: "0.9.1",
              nightly_required: found.upstream.nightly_required ?? false,
              docker_image: found.upstream.docker_image ?? null,
              tensor_parallel: found.upstream.tensor_parallel,
              default_strategy: null,
              deployments: [],
              replica_memory_mib_min: null,
              context_length: 131072,
              max_model_len: 131072,
              kv_cache_dtype: "fp8",
              tool_call_parser: found.upstream.tool_call_parser,
              reasoning_parser: null,
              args: [],
              env: {},
              features_on: [],
              features_opt_in: [],
              strategies: [],
              requires_single_topology_domain: true,
              verified_hardware: found.upstream.verified_hardware,
            }
          : null,
        publisher: {
          ...emptyUpstream(),
          provider: slug.split("/")[0],
          provider_model_id: slug,
          context_length: 131072,
          input_modalities: ["text"],
          licence: found?.licence ?? null,
          source_uri: `https://huggingface.co/${slug}`,
          recorded_by: "huggingface",
          recorded_at: new Date().toISOString(),
        },
        existing_id: artifacts.find((a) => a.digest === digest)?.id ?? null,
        notes: [
          {
            level: "info",
            message:
              "the device shape is sized from config.json — a suggestion, not a measurement",
          },
        ],
      }
    },
  },
])

function commitOf(slug: string): string {
  let hash = 0
  for (const char of slug) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return hash.toString(16).padStart(8, "0").repeat(5).slice(0, 40)
}

collection({
  path: "/artifacts",
  singular: "artifact",
  rows: () => artifacts,
  names: (a) => [a.name, a.digest],
  searchable: (a) => [a.name, a.digest],
  filter: (a, q) =>
    eq(q.get("kind"), a.kind) &&
    eq(q.get("format"), a.format) &&
    eq(q.get("project"), a.project_id),
})

/** The parent of a deployment, or undefined if the corpus is inconsistent. */
const parentOf = (deployment: Deployment) =>
  workloads.find((w) => w.id === deployment.workload_id)

collection({
  path: "/workloads",
  singular: "workload",
  rows: () => workloads,
  names: (w) => [w.name],
  searchable: (w) => [w.name],
  labels: (w) => w.labels,
  filter: (w, q) =>
    eq(q.get("project"), w.project_id) &&
    eq(q.get("kind"), w.kind) &&
    eq(q.get("phase"), w.phase) &&
    eq(q.get("artifact"), w.serving.artifact_id) &&
    eq(q.get("class"), w.serving.shape.device_class) &&
    eq(q.get("serving_digest"), w.serving_digest) &&
    (q.get("cluster") === undefined ||
      w.deployments.some((d) =>
        [d.cluster_id, d.cluster_name].includes(
          clusterRef(q.get("cluster")) ?? q.get("cluster")!
        )
      )),
})

/**
 * Project, phase and labels are workload facts read through the parent: a
 * deployment carries its own admission phase, and `?phase=` asks about that.
 */
collection({
  path: "/deployments",
  singular: "deployment",
  rows: () => deployments,
  names: (d) => [d.name],
  labels: (d) => parentOf(d)?.labels ?? {},
  filter: (d, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), d.cluster_id) &&
    eq(q.get("workload"), d.workload_id, d.workload_name) &&
    eq(q.get("project"), parentOf(d)?.project_id ?? null) &&
    eq(q.get("phase"), d.admission_phase),
})

collection({
  path: "/endpoints",
  singular: "endpoint",
  rows: () => endpoints,
  names: (e) => [e.name],
  searchable: (e) => [e.name, e.slug],
  labels: (e) => e.labels,
  filter: (e, q) => eq(q.get("project"), e.project_id),
})

collection({
  path: "/tenants",
  singular: "tenant",
  rows: () => tenants,
  names: (t) => [t.slug],
  searchable: (t) => [t.slug, t.name],
  filter: (t, q) => bool(q.bool("enabled"), t.enabled),
})

collection({
  path: "/projects",
  singular: "project",
  rows: () => projects,
  names: (p) => [p.slug],
  searchable: (p) => [p.slug, p.name],
  filter: (p, q) =>
    eq(q.get("tenant"), p.tenant.id, p.tenant.slug) &&
    bool(q.bool("enabled"), p.enabled),
})

collection({
  path: "/quotas",
  singular: "quota",
  rows: () => quotas,
  filter: (r, q) =>
    eq(q.get("project"), r.project, r.project_slug) &&
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), r.cluster) &&
    eq(q.get("class"), r.device_class) &&
    bool(q.bool("over_quota_enabled"), r.over_quota_enabled),
})

collection({
  path: "/priority-classes",
  singular: "priority class",
  rows: () => priorityClasses,
  names: (p) => [p.name],
  searchable: (p) => [p.name, p.description],
  filter: (p, q) =>
    bool(q.bool("preemptible"), p.preemptible) &&
    bool(q.bool("preempts"), p.preempts),
})

collection({
  path: "/policies",
  singular: "policy",
  rows: () => policies,
  names: (p) => [p.name],
  searchable: (p) => [p.name, p.description],
  filter: (p, q) =>
    bool(q.bool("enabled"), p.enabled) &&
    (q.get("region") === undefined ||
      p.allowed_regions.includes(q.get("region")!)) &&
    (q.get("class") === undefined ||
      p.allowed_device_classes.includes(q.get("class")!)),
})

collection({
  path: "/allocations",
  singular: "allocation",
  rows: () => allocations,
  filter: (a, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), a.cluster) &&
    eq(q.get("project"), a.project, a.project_slug) &&
    eq(q.get("class"), a.device_class) &&
    eq(q.get("phase"), a.phase) &&
    bool(q.bool("live"), a.live) &&
    bool(q.bool("over_quota"), a.over_quota) &&
    (q.get("device") === undefined ||
      a.devices.some((d) => d.device === q.get("device"))),
})

collection({
  path: "/components",
  singular: "component",
  rows: () => components,
  names: (c) => [c.name],
  searchable: (c) => [c.name, c.kind],
  filter: (c, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), c.cluster_id) &&
    eq(q.get("name"), c.name) &&
    eq(q.get("kind"), c.kind) &&
    bool(q.bool("behind"), c.behind) &&
    bool(q.bool("enabled"), c.enabled),
})

collection({
  path: "/events",
  singular: "event",
  rows: () => events,
  filter: (e, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), e.cluster_id) &&
    eq(q.get("kind"), e.kind) &&
    eq(q.get("severity"), e.severity) &&
    eq(q.get("subject"), e.subject.id) &&
    (q.get("since") === undefined || e.occurred_at >= q.get("since")!) &&
    (q.get("until") === undefined || e.occurred_at <= q.get("until")!),
})

const operationsCollection = collection({
  path: "/operations",
  singular: "operation",
  rows: () => operations,
  filter: (o, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), o.cluster_id) &&
    eq(q.get("type"), o.type) &&
    eq(q.get("phase"), o.phase) &&
    eq(q.get("target"), o.target.id) &&
    eq(q.get("actor"), o.requested_by),
})

/**
 * Runs, and the harness catalogue the form is built from.
 *
 * The catalogue is duplicated here rather than derived from anything: offline
 * mode is a copy of the plane's answers, and the plane is where that table
 * actually lives. If the two drift, the demo shows a field the real plane would
 * refuse — which is a bug in the demo and not in the design.
 */
const benchmarksCollection = collection({
  path: "/benchmarks",
  singular: "benchmark run",
  rows: () => benchmarkRuns,
  filter: (b, q) =>
    eq(q.get("deployment"), b.deployment_id) &&
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), b.cluster_id) &&
    eq(q.get("harness"), b.harness) &&
    eq(q.get("phase"), b.phase) &&
    eq(q.get("actor"), b.requested_by),
})

const HARNESSES: BenchmarkHarness[] = [
  {
    name: "aiperf",
    image: "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0",
    description:
      "NVIDIA AIPerf, the successor to GenAI-Perf. Closed-loop, concurrency-first.",
    levels: ["deployment", "workload"],
    max_probes: null,
    parameters: [
      {
        name: "concurrency",
        type: "int",
        default: 10,
        description: "Requests in flight at once.",
        levels: [],
      },
      {
        name: "request_count",
        type: "int",
        default: 100,
        description: "Total requests to send.",
        levels: [],
      },
      {
        name: "input_tokens_mean",
        type: "int",
        default: 2000,
        description: "Mean synthetic prompt length, in tokens.",
        levels: [],
      },
      {
        name: "input_tokens_stddev",
        type: "int",
        default: 0,
        description: "Spread of the prompt length.",
        levels: [],
      },
      {
        name: "output_tokens_mean",
        type: "int",
        default: 256,
        description: "Mean generation length, in tokens.",
        levels: [],
      },
      {
        name: "output_tokens_stddev",
        type: "int",
        default: 0,
        description: "Spread of the generation length.",
        levels: [],
      },
      {
        name: "endpoint_type",
        type: "str",
        default: "chat",
        description: "AIPerf endpoint type: chat, completions, embeddings.",
        levels: [],
      },
      {
        name: "streaming",
        type: "bool",
        default: true,
        description: "Measure TTFT and ITL, which needs a streamed response.",
        levels: [],
      },
      {
        name: "warmup_request_count",
        type: "int",
        default: 10,
        description: "Requests sent and discarded before measuring.",
        levels: [],
      },
      {
        name: "extra",
        type: "str",
        default: "",
        description: "Extra flags, appended verbatim to the command line.",
        levels: [],
      },
    ],
  },
]

const TARGET_KINDS: Record<string, string> = {
  nod_: "node",
  dev_: "device",
  art_: "artifact",
  dep_: "deployment",
  wkl_: "workload",
  cmp_: "component",
}

/** Workload operations are plane-local: they name no cluster and never dispatch. */
const PLANE_LOCAL_PREFIXES = ["wkl_"]

function clusterOf(target: string): string | null {
  return (
    nodes.find((n) => n.id === target)?.cluster ??
    devices.find((d) => d.id === target)?.cluster ??
    deployments.find((d) => d.id === target)?.cluster_id ??
    components.find((c) => c.id === target)?.cluster_id ??
    null
  )
}

registerFixtures([
  {
    method: "GET",
    path: "/benchmarks/harnesses",
    handler: () => ({
      items: HARNESSES,
      count: HARNESSES.length,
      next_cursor: null,
    }),
  },
  {
    /**
     * Registered before `/benchmarks/:ref` so the fixture router does not read
     * `serving` as a run id, the same ordering `/artifacts/resolve` needs.
     *
     * An unknown digest answers zero rather than 404: the question is how much
     * measurement stands behind a configuration, and "none" is an answer.
     */
    method: "GET",
    path: "/benchmarks/serving/:digest",
    handler: ({ params }: FixtureContext) => {
      const runs = benchmarkRuns.filter(
        (run) => run.provenance.serving_digest === params.digest
      )
      const graded = runs.filter((run) => run.phase === "succeeded")
      const peaks = graded
        .map((run) => {
          const rate = run.summary?.["output_token_throughput"]
          const value = (rate as { avg?: number } | undefined)?.avg
          return typeof value === "number" ? value : null
        })
        .filter((value): value is number => value !== null)
      return {
        serving_digest: params.digest,
        run_count: runs.length,
        graded_run_count: graded.length,
        peak_output_tokens_per_second: peaks.length ? Math.max(...peaks) : null,
      }
    },
  },
  {
    method: "POST",
    path: "/benchmarks",
    handler: ({ body }: FixtureContext) => {
      const data = body as BenchmarkCreateRequest
      const harness = HARNESSES.find((h) => h.name === data.harness)
      if (!harness) {
        throw new ConflictError(
          "invalid",
          `unknown benchmark harness '${data.harness}'`,
          { allowed: HARNESSES.map((h) => h.name) }
        )
      }
      const deployment = deployments.find((d) => d.id === data.deployment)
      if (!deployment) {
        throw new NotFoundError(`no deployment '${data.deployment}'`)
      }
      const known = new Set(harness.parameters.map((p) => p.name))
      const unknown = Object.keys(data.parameters ?? {}).filter(
        (key) => !known.has(key)
      )
      if (unknown.length > 0) {
        throw new ConflictError(
          "invalid",
          `${harness.name} does not accept ${unknown.join(", ")}`,
          { harness: harness.name, unknown }
        )
      }
      const parameters: Record<string, unknown> = {}
      for (const parameter of harness.parameters) {
        parameters[parameter.name] =
          data.parameters?.[parameter.name] ?? parameter.default
      }
      const now = new Date().toISOString()
      const created: BenchmarkRun = {
        id: uid("bmk_"),
        request_id: null,
        deployment_id: deployment.id,
        cluster_id: deployment.cluster_id,
        endpoint_id: null,
        operation_id: null,
        harness: harness.name,
        image: data.image?.trim() || harness.image,
        harness_version: null,
        parameters,
        step_index: 0,
        vantage: null,
        discard: false,
        requested_by: "operator",
        reason: data.reason ?? null,
        provenance: {
          model: deployment.name,
          deployment_generation: deployment.generation,
          serving_digest: parentOf(deployment)?.serving_digest ?? null,
          engine_args: {},
          serving: {},
        },
        phase: "pending",
        summary: null,
        artifacts_uri: null,
        error: null,
        created_at: now,
        updated_at: null,
        started_at: null,
        finished_at: null,
      }
      benchmarkRuns.unshift(created)
      return created
    },
  },
  {
    method: "POST",
    path: "/operations",
    handler: ({ body, options }: FixtureContext) => {
      const data = body as OperationCreateRequest
      const prefix = OPERATION_TYPES[data.type as keyof typeof OPERATION_TYPES]
      if (!prefix) {
        throw new ConflictError(
          "invalid",
          `unknown operation type '${data.type}'`,
          {
            allowed: Object.keys(OPERATION_TYPES).sort(),
          }
        )
      }
      if (!data.target.startsWith(prefix)) {
        throw new ConflictError(
          "invalid",
          `${data.type} acts on ${TARGET_KINDS[prefix]} targets, not '${data.target}'`,
          { type: data.type, expected_prefix: prefix, target: data.target }
        )
      }
      const key = options.idempotencyKey
      if (key) {
        const replay = operations.find((o) => o.parameters.__key === key)
        if (replay) return replay
      }
      const planeLocal = PLANE_LOCAL_PREFIXES.includes(prefix)
      const cluster = planeLocal
        ? null
        : (clusterOf(data.target) ??
          clusterRef(data.cluster ?? undefined) ??
          null)
      if (!planeLocal && cluster === null) {
        throw new ConflictError(
          "invalid",
          `${data.type} needs a cluster: its target does not belong to one`,
          { type: data.type, target: data.target }
        )
      }
      const created: Operation = {
        id: uid("opr_"),
        cluster_id: cluster,
        type: data.type,
        target: { type: TARGET_KINDS[prefix], id: data.target },
        reason: data.reason ?? null,
        requested_by: "operator",
        parameters: key
          ? { ...data.parameters, __key: key }
          : (data.parameters ?? {}),
        deadline_at: data.deadline ?? null,
        created_at: new Date().toISOString(),
        updated_at: null,
        phase: "pending",
        result: null,
        error: null,
        attempts: 0,
        cancel_requested_at: null,
        started_at: null,
        finished_at: null,
      }
      operations.unshift(created)
      return created
    },
  },
  {
    method: "DELETE",
    path: "/operations/:ref",
    handler: ({ params }: FixtureContext) => {
      const operation = operations.find((o) => o.id === params.ref)
      if (!operation) throw new NotFoundError(`no operation '${params.ref}'`)
      if (!isOpen(operation.phase)) {
        throw new ConflictError(
          "conflict",
          `operation '${operation.id}' already finished as '${operation.phase}'`,
          { phase: operation.phase }
        )
      }
      const now = new Date().toISOString()
      operation.cancel_requested_at = now
      operation.updated_at = now
      if (operation.phase === "pending") {
        operation.phase = "cancelled"
        operation.finished_at = now
      }
      return operation
    },
  },
])

collection({
  path: "/discovered",
  singular: "discovered workload",
  rows: () => discoveredWorkloads,
  searchable: (w) => [w.name, w.namespace, w.model],
  labels: (w) => w.labels,
  filter: (w, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), w.cluster_id) &&
    eq(q.get("namespace"), w.namespace) &&
    eq(q.get("status"), w.status) &&
    // Removed sightings leave the default listing and stay reachable by asking
    // for them, exactly as the plane filters them.
    (!!q.get("status") || w.status !== "dismissed"),
})

function sighting(ref: string): DiscoveredWorkload {
  const found = discoveredWorkloads.find((w) => w.id === ref)
  if (!found) throw new NotFoundError(`no discovered workload '${ref}'`)
  return found
}

/**
 * Track and ignore write the decision beside the sighting and nothing else.
 * Promote is the only one that produces declared state, and it refuses on a
 * stale fingerprint the way the plane does — that check is the whole safety of
 * adopting a spec we do not own.
 */
function decide(
  ref: string,
  decision: "tracked" | "ignored" | "dismissed",
  body: unknown
) {
  const workload = sighting(ref)
  if (workload.status === "promoted") {
    throw new ConflictError(
      "invalid",
      `'${workload.name}' is already declared as ${workload.deployment_id}`
    )
  }
  const data = (body ?? {}) as DecisionRequest
  workload.status = decision
  workload.project_id = data.project ?? workload.project_id
  workload.note = data.note ?? null
  workload.decided_by = "operator"
  workload.decided_at = new Date().toISOString()
  return workload
}

registerFixtures([
  {
    method: "GET",
    path: "/discovered/:ref/serving-draft",
    handler: ({ params }: FixtureContext) => servingDraft(sighting(params.ref)),
  },
])

/**
 * One reading of a sighting's argv, shared by the draft route and by promote.
 *
 * Two readings would be two answers: the plane derives the block it adopts from
 * the same parse it showed, so a fixture that drafted one thing and promoted
 * another would be the one bug this flow exists to make impossible.
 */
function servingDraft(workload: DiscoveredWorkload): ServingDraft {
  const args: Record<string, string> = {}
  const list = workload.spec?.args ?? []
  for (let i = 0; i < list.length; i++) {
    if (!list[i].startsWith("--")) continue
    const next = list[i + 1]
    args[list[i].slice(2)] = next && !next.startsWith("--") ? next : ""
    if (next && !next.startsWith("--")) i++
  }
  const match = /\/([a-z0-9]+)-runtime:([^@\s]+)/.exec(
    workload.spec?.image ?? ""
  )
  const model = args["model"] ?? null
  const context = args["max-model-len"] ? Number(args["max-model-len"]) : null
  return {
    artifact_ref: model ? `hf:${model}` : null,
    artifact_name: model,
    name: args["served-model-name"] ?? workload.name,
    engine: match?.[1] ?? null,
    engine_runtime_version: match?.[2] ?? null,
    engine_runtime_image: workload.spec?.image ?? null,
    engine_args: {},
    imported_args: args,
    context_length: context,
    tensor_parallel: Number(args["tensor-parallel-size"] ?? 1),
    pipeline_parallel: 1,
    expert_parallel: 1,
    max_batch_tokens: args["max-num-seqs"]
      ? Number(args["max-num-seqs"])
      : null,
    kv_cache_fraction: args["gpu-memory-utilization"]
      ? Number(args["gpu-memory-utilization"])
      : null,
    quantization: null,
    dtype: null,
    device_count: workload.devices_per_replica,
    device_class: "h100-sxm",
    cpu_cores: null,
    memory_mib: null,
    confident: !!match && !!model && context !== null,
    notes: match ? [] : ["could not tell which engine this runs"],
    artifact: {
      reference: model ? `hf:${model}` : null,
      digest: model ? `hf:${model}` : null,
      name: model,
      kind: "weights",
      format: null,
      revision: args["revision"] ?? null,
      parameter_count: null,
      source_scheme: model ? "hf" : null,
      source_uri: model ? `hf://${model}` : null,
      verified: false,
      metadata: { source: "discovered" },
      existing_id:
        artifacts.find((a) => a.digest === `hf:${model}`)?.id ?? null,
      notes: model
        ? ["the digest is this reference, not a hash of the bytes"]
        : ["no --model argument, so there are no weights to identify"],
    },
  }
}

registerFixtures([
  {
    method: "POST",
    path: "/discovered/:ref/track",
    handler: ({ params, body }: FixtureContext) =>
      decide(params.ref, "tracked", body),
  },
  {
    method: "POST",
    path: "/discovered/:ref/ignore",
    handler: ({ params, body }: FixtureContext) =>
      decide(params.ref, "ignored", body),
  },
  {
    method: "DELETE",
    path: "/discovered/:ref",
    handler: ({ params }: FixtureContext) =>
      decide(params.ref, "dismissed", {}),
  },
  {
    method: "POST",
    path: "/discovered/:ref/restore",
    handler: ({ params }: FixtureContext) => {
      const workload = sighting(params.ref)
      workload.status = "new"
      workload.decided_by = null
      workload.decided_at = null
      workload.note = null
      return workload
    },
  },
  {
    method: "POST",
    path: "/discovered/:ref/promote",
    handler: ({ params, body }: FixtureContext) => {
      const workload = sighting(params.ref)
      const data = body as PromoteRequest
      if (workload.spec_fingerprint !== data.fingerprint) {
        throw new ConflictError(
          "conflict",
          `'${workload.name}' has changed since it was read`,
          { expected: data.fingerprint, actual: workload.spec_fingerprint }
        )
      }
      const project = projects.find(
        (p) => p.id === data.project || p.slug === data.project
      )
      if (!project) throw new NotFoundError(`no project '${data.project}'`)
      const read = servingDraft(workload)

      const now = new Date().toISOString()
      const name = data.name || workload.name
      const parentId = uid("wkl_")
      const cluster = clusters.find((c) => c.id === workload.cluster_id)
      const created: Deployment = {
        id: uid("dep_"),
        name,
        workload_id: parentId,
        workload_name: name,
        cluster_id: workload.cluster_id,
        min_replicas: workload.replicas_desired,
        max_replicas: workload.replicas_desired,
        desired_replicas: workload.replicas_desired,
        admission_phase: "running",
        admission_reason: null,
        queued_at: now,
        admitted_at: now,
        placed_at: now,
        generation: 1,
        enabled: true,
        created_at: now,
        updated_at: null,
        revision: revision(now),
        adopted_workload_id: workload.id,
        adopted_fingerprint: workload.spec_fingerprint,
        authority: "managed",
        status: {
          observed_generation: 1,
          phase: "running",
          reason: null,
          message: null,
          desired_replicas: workload.replicas_desired,
          ready_replicas: workload.replicas_ready,
          updated_replicas: workload.replicas_ready,
          unavailable_replicas:
            workload.replicas_desired - workload.replicas_ready,
          last_error_code: null,
          last_error_message: null,
          last_error_at: null,
          observed_at: now,
        },
        replicas: {
          total: workload.replicas_desired,
          ready: workload.replicas_ready,
          running: workload.replicas_ready,
          pending: workload.replicas_desired - workload.replicas_ready,
          failed: 0,
          at_current_generation: workload.replicas_ready,
        },
      }
      const parent: Workload = {
        id: parentId,
        name,
        served_model_name: name,
        kind: "service",
        phase: "running",
        project_id: project.id,
        priority_class:
          data.priority_class ?? project.default_priority_class ?? "default",
        preemptible: false,
        paused: false,
        gang_size: 1,
        /**
         * Read off the sighting, not off the request. `artifact` is the only part
         * of the configuration a caller states, because it is the only part the
         * cluster cannot tell us — everything else is what is already running, and
         * declaring something else would restart the model the adoption exists to
         * leave alone.
         */
        serving_digest: `svg_${"0".repeat(64)}`,
        serving: {
          artifact_id: data.artifact,
          engine: read.engine ?? "vllm",
          engine_runtime_version: read.engine_runtime_version ?? "latest",
          engine_runtime_image: read.engine_runtime_image,
          frontend_image: null,
          engine_args: {},
          imported_args: read.imported_args,
          quantization: read.quantization,
          dtype: read.dtype,
          parallelism: {
            tensor: read.tensor_parallel,
            pipeline: read.pipeline_parallel,
            expert: read.expert_parallel,
          },
          context_length: read.context_length ?? 0,
          max_batch_tokens: read.max_batch_tokens,
          cache: {
            kv_cache_dtype: null,
            kv_cache_fraction: read.kv_cache_fraction,
            prefix_cache_enabled: true,
            prefix_cache_config: {},
          },
          serving: emptyServing(),
          capabilities: emptyCapabilities(),
          resolved_engine_args: {},
          shape: {
            device_class: read.device_class ?? "",
            device_count: read.device_count,
            device_memory_mib_min: 0,
            requires_single_topology_domain: false,
            topology_domain_kind: null,
          },
          resources: {
            cpu_cores: read.cpu_cores,
            memory_mib: read.memory_mib,
            disk_gib: null,
          },
          overlay: { pod_spec: {}, dynamo: {} },
        },
        artifact_tag: null,
        adapter_artifact_ids: [],
        overrides: {},
        labels: workload.labels,
        strategy: "pack",
        cluster_floors: {},
        max_clusters: 1,
        pinned_cluster_id: workload.cluster_id,
        scale: {
          unit: "replicas",
          min: workload.replicas_desired,
          max: workload.replicas_desired,
          desired: workload.replicas_desired,
          placed: workload.replicas_desired,
          ready: workload.replicas_ready,
        },
        started_at: now,
        finished_at: null,
        deadline_at: null,
        created_at: now,
        updated_at: null,
        revision: revision(now),
        deployments: [
          {
            id: created.id,
            name: created.name,
            cluster_id: created.cluster_id,
            cluster_name: cluster?.name ?? created.cluster_id,
            region: cluster?.region ?? null,
            min_replicas: created.min_replicas,
            max_replicas: created.max_replicas,
            desired_replicas: created.desired_replicas,
            admission_phase: created.admission_phase,
            admission_reason: created.admission_reason,
            generation: created.generation,
            enabled: created.enabled,
            authority: created.authority,
            queued_at: created.queued_at,
            admitted_at: created.admitted_at,
            placed_at: created.placed_at,
            replicas: created.replicas,
            status: created.status,
          },
        ],
      }
      workloads.unshift(parent)
      deployments.unshift(created)
      workload.status = "promoted"
      workload.deployment_id = created.id
      workload.project_id = project.id
      workload.decided_by = "operator"
      workload.decided_at = now
      return created
    },
  },
])

export { operationsCollection, benchmarksCollection }

/**
 * The plane's effective configuration. Static: it describes a process, and the
 * offline plane has none — the values are a plausible deployment. `fra-1` is
 * deliberately left out of `metrics.sources`, so the unmapped-cluster marker on
 * the Settings › Configuration panel has something to show.
 */
registerFixtures([
  {
    method: "GET",
    path: "/system/config",
    handler: (): PlaneConfig => ({
      app: {
        env: "production",
        debug: false,
        log_level: "INFO",
        log_format: "json",
        allowed_origins: ["https://hangar.example.com"],
      },
      api: { host: "0.0.0.0", port: 8000 },
      hub: {
        host: "0.0.0.0",
        port: 8001,
        path: "/v1/hub/sync",
        public_host: "hangar.example.com",
        public_url: "wss://hangar.example.com/v1/hub/sync",
        instance_id: "",
        session_ttl_seconds: 600,
        reaper_interval_seconds: 60,
        ownership_check_seconds: 10,
      },
      reactor: {
        enabled: true,
        interval_seconds: 60,
        lease_seconds: 180,
        event_batch: 500,
        notify_timeout: 5,
      },
      database: {
        driver: "postgresql+psycopg",
        host: "hangar-db.internal",
        port: 5432,
        database: "hangar",
        echo: false,
        pool_size: 10,
        pool_max_overflow: 10,
        pool_pre_ping: true,
      },
      metrics: {
        timeout: 5,
        window: "5m",
        step: "60s",
        shared_url: "https://prometheus.example.com",
        remote_write_url: "https://metrics.example.com/api/v1/write",
        remote_write_auth_secret: "platform/prom-remote-write-auth",
        sources: [
          {
            cluster: "iad-1",
            url: "https://prometheus.example.com",
            kind: "prometheus",
            label: "iad-1",
          },
          {
            cluster: "sjc-1",
            url: "https://prometheus.example.com",
            kind: "prometheus",
            label: "cluster-sjc",
          },
          {
            cluster: "ams-1",
            url: "https://prom.ams.internal:9090",
            kind: "prometheus",
            label: "",
          },
        ],
      },
      auth: {
        admin_auth: "enabled",
        admin_password_hash_set: true,
        session_signing_secret_set: true,
        session_ttl_seconds: 900,
        operator_token_set: true,
        huggingface_token_set: false,
      },
      setup: {
        operator_image: "ghcr.io/hangar-dev/hangar-operator:cf143b3",
        chart_ref: "oci://ghcr.io/hangar-dev/charts/cluster-setup",
        chart_version: "0.4.2",
        chart_private: true,
      },
    }),
  },
])

/**
 * `GET /placement/reservations`, derived rather than stored beside the corpus.
 *
 * The plane's own definition is `max(0, declared - observed)` per deployment:
 * the devices the deployment asked its cluster for, less the allocation devices
 * ingestion has confirmed the cluster actually bound. That subtraction cannot be
 * performed here, because the demo allocations are illustrative and were never
 * meant to account for every replica the demo deployments declare — running it
 * anyway makes almost every deployment look like it is owed its whole size, and
 * a fleet where every cluster reports zero available is a fleet nobody can learn
 * the screen from.
 *
 * So the corpus's own replica rollup stands in for what has been bound: replicas
 * declared, less replicas the cluster reports ready, times the workload's device
 * shape. It is the same quantity — capacity promised and not yet converged —
 * counted from the one book the offline fleet keeps consistently.
 */
function reservations(query: FixtureQuery) {
  const cluster = query.get("cluster")
  const deviceClass = query.get("device_class")
  const rows = []

  for (const deployment of deployments) {
    if (!deployment.enabled) continue
    const workload = parentOf(deployment)
    if (!workload) continue
    const shape = workload.serving.shape

    const pending = Math.max(
      0,
      deployment.desired_replicas - deployment.replicas.ready
    )
    const owed = pending * shape.device_count
    if (owed === 0) continue

    const named = clusters.find((c) => c.id === deployment.cluster_id)
    rows.push({
      cluster: deployment.cluster_id,
      cluster_name: named?.name ?? deployment.cluster_id,
      project: workload.project_id,
      workload: workload.id,
      workload_name: workload.name,
      deployment: deployment.id,
      deployment_name: deployment.name,
      device_class: shape.device_class,
      devices: owed,
      requires_single_domain: shape.requires_single_topology_domain,
      domain_kind: shape.topology_domain_kind,
    })
  }

  return rows.filter(
    (row) =>
      eq(cluster, row.cluster, row.cluster_name) &&
      eq(deviceClass, row.device_class)
  )
}

registerFixtures([
  {
    method: "GET",
    path: "/placement/reservations",
    handler: ({ query }: FixtureContext) => reservations(query),
  },
])
