import { registerFixtures, type FixtureContext } from "@/lib/api/fixtures"
import { ConflictError, NotFoundError } from "@/lib/api/transport"
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
  recipes,
  workloads,
} from "./data"
import { revision, uid } from "./ids"
import { collection } from "./registry"
import type { Deployment, Workload } from "@/modules/demand/types"
import type {
  BenchmarkCreateRequest,
  BenchmarkHarness,
  BenchmarkRun,
} from "@/modules/benchmarks/types"
import type {
  DecisionRequest,
  DiscoveredWorkload,
  PromoteRequest,
} from "@/modules/discovered/types"

const bool = (value: boolean | undefined, actual: boolean) =>
  value === undefined || value === actual

const eq = (want: string | undefined, ...actual: (string | null)[]) =>
  want === undefined || actual.includes(want)

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
  { format: string; size: number; parameters: number; licence: string }
> = {
  "openai/gpt-oss-20b": {
    format: "safetensors",
    size: 41_231_686_042,
    parameters: 20_900_000_000,
    licence: "apache-2.0",
  },
  "Qwen/Qwen3-32B": {
    format: "safetensors",
    size: 65_527_486_976,
    parameters: 32_800_000_000,
    licence: "apache-2.0",
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
        recipe: {
          device_count: 1,
          device_memory_mib_min: 81920,
          context_length: 131072,
          quantization: null,
          dtype: "bfloat16",
        },
        existing_id: artifacts.find((a) => a.digest === digest)?.id ?? null,
        notes: ["Sized from config.json — a suggestion, not a measurement."],
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

collection({
  path: "/recipes",
  singular: "recipe",
  rows: () => recipes,
  names: (r) => [r.name],
  searchable: (r) => [r.name, r.engine],
  labels: (r) => r.labels,
  filter: (r, q) =>
    eq(q.get("artifact"), r.artifact_id) &&
    eq(q.get("engine"), r.engine) &&
    eq(q.get("class"), r.shape.device_class),
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
    eq(q.get("recipe"), w.recipe_id) &&
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
  searchable: (e) => [e.name, e.display_name],
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
          recipe_id: parentOf(deployment)?.recipe_id ?? "",
          recipe_version: null,
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
        : (clusterOf(data.target) ?? clusterRef(data.cluster ?? undefined) ?? null)
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
    path: "/discovered/:ref/recipe-draft",
    handler: ({ params }: FixtureContext) => {
      const workload = sighting(params.ref)
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
      const context = args["max-model-len"]
        ? Number(args["max-model-len"])
        : null
      return {
        artifact_ref: model ? `hf:${model}` : null,
        artifact_name: model,
        name: args["served-model-name"] ?? workload.name,
        engine: match?.[1] ?? null,
        engine_version: match?.[2] ?? null,
        engine_image: workload.spec?.image ?? null,
        engine_args: {},
        imported_args: {},
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
    },
  },
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
      const recipe = recipes.find(
        (r) => r.id === data.recipe || r.name === data.recipe
      )
      if (!recipe) throw new NotFoundError(`no recipe '${data.recipe}'`)
      const project = projects.find(
        (p) => p.id === data.project || p.slug === data.project
      )
      if (!project) throw new NotFoundError(`no project '${data.project}'`)

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
        kind: "service",
        phase: "running",
        project_id: project.id,
        priority_class:
          data.priority_class ?? project.default_priority_class ?? "default",
        preemptible: false,
        paused: false,
        gang_size: 1,
        recipe_id: recipe.id,
        adapter_artifact_ids: [],
        overrides: {},
        labels: workload.labels,
        requires_single_topology_domain: null,
        topology_domain_kind: null,
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
