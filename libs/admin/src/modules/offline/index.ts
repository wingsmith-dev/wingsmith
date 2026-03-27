import {
  registerFixtures,
  type FixtureContext,
} from "@/lib/api/fixtures"
import {
  ConflictError,
  NotFoundError,
} from "@/lib/api/transport"
import {
  OPERATION_TYPES,
  isOpen,
  type Operation,
  type OperationCreateRequest,
} from "@/modules/machinery/types"
import {
  allocations,
  artifacts,
  clusters,
  components,
  deployments,
  devices,
  domains,
  endpoints,
  events,
  nodes,
  operations,
  policies,
  pools,
  priorityClasses,
  projects,
  quotas,
  recipes,
} from "./data"
import { uid } from "./ids"
import { collection } from "./registry"

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
  filter: (c, q) => eq(q.get("region"), c.region) && eq(q.get("phase"), c.phase),
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

collection({
  path: "/deployments",
  singular: "deployment",
  rows: () => deployments,
  names: (d) => [d.name],
  labels: (d) => d.labels,
  filter: (d, q) =>
    eq(clusterRef(q.get("cluster")) ?? q.get("cluster"), d.cluster_id) &&
    eq(q.get("recipe"), d.recipe_id) &&
    eq(q.get("project"), d.workload.project_id) &&
    eq(q.get("phase"), d.workload.phase) &&
    eq(q.get("type"), d.workload.type),
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
  path: "/projects",
  singular: "project",
  rows: () => projects,
  names: (p) => [p.slug],
  searchable: (p) => [p.slug, p.name],
  filter: (p, q) =>
    eq(q.get("tenant"), p.tenant.id, p.tenant.slug) && bool(q.bool("enabled"), p.enabled),
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
    bool(q.bool("preemptible"), p.preemptible) && bool(q.bool("preempts"), p.preempts),
})

collection({
  path: "/policies",
  singular: "policy",
  rows: () => policies,
  names: (p) => [p.name],
  searchable: (p) => [p.name, p.description],
  filter: (p, q) =>
    bool(q.bool("enabled"), p.enabled) &&
    (q.get("region") === undefined || p.allowed_regions.includes(q.get("region")!)) &&
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

const TARGET_KINDS: Record<string, string> = {
  nod_: "node",
  dev_: "device",
  art_: "artifact",
  dep_: "deployment",
  cmp_: "component",
}

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
    method: "POST",
    path: "/operations",
    handler: ({ body, options }: FixtureContext) => {
      const data = body as OperationCreateRequest
      const prefix = OPERATION_TYPES[data.type as keyof typeof OPERATION_TYPES]
      if (!prefix) {
        throw new ConflictError("invalid", `unknown operation type '${data.type}'`, {
          allowed: Object.keys(OPERATION_TYPES).sort(),
        })
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
      const cluster = clusterOf(data.target) ?? clusterRef(data.cluster ?? undefined) ?? null
      if (cluster === null) {
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
        parameters: key ? { ...data.parameters, __key: key } : (data.parameters ?? {}),
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

export { operationsCollection }
