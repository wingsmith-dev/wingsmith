import type { StatusTone } from "@/components/layout/status-dot"
import { roleReadiness, rolesReady } from "@/modules/demand/topology"
import type {
  Artifact,
  ArtifactUpstream,
  EndpointSurface,
  PolicyRule,
  ServingCapabilities,
  ServingReasoning,
  ServingBehaviour,
  Workload,
  WorkloadDeployment,
} from "./types"

const WORKLOAD_PHASE_TONES: Record<string, StatusTone> = {
  pending: "neutral",
  admitted: "info",
  placed: "info",
  running: "ok",
  failed: "danger",
}

/**
 * The same tones answer for a workload's derived phase and for a deployment's
 * `admission_phase`: they are the same closed set read at two altitudes.
 */
export function workloadPhaseTone(phase: string): StatusTone {
  return WORKLOAD_PHASE_TONES[phase] ?? "warn"
}

export const WORKLOAD_PHASES = ["pending", "admitted", "placed", "running"]
export const WORKLOAD_KINDS = ["service", "task", "gang"]
export const WORKLOAD_STRATEGIES = ["pack", "spread"]

const SCALE_UNITS: Record<string, string> = {
  service: "replicas",
  task: "completions",
  gang: "ranks",
}

/**
 * What a workload's three scale numbers count. The plane sends this on
 * `scale.unit`; this is the answer for a form that has no workload yet.
 */
export function scaleUnit(kind: string): string {
  return SCALE_UNITS[kind] ?? "replicas"
}

/**
 * Whether a workload may hold a share on more than one cluster.
 *
 * A property of the kind, not a field. A gang's ranks are co-scheduled, so a
 * split gang is broken rather than degraded and placement rejects it outright —
 * which is why the form hides fan-out for one instead of letting it be asked for.
 */
export function splittable(kind: string): boolean {
  return kind !== "gang"
}

export function formatBytes(bytes: number): string {
  if (bytes <= 0) return "—"
  const units = ["B", "KiB", "MiB", "GiB", "TiB"]
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`
}

export function formatParameters(count: number | null): string {
  if (count === null) return "—"
  if (count >= 1e9) return `${(count / 1e9).toFixed(1)}B`
  if (count >= 1e6) return `${(count / 1e6).toFixed(0)}M`
  return String(count)
}

/**
 * The source this artifact can be re-read from, or null.
 *
 * Mirrors the plane's own precedence — `metadata.huggingface_id` first, then an
 * `hf://` location with its revision dropped — so the refresh button is disabled
 * exactly when the plane would refuse. Deciding that here rather than by trying the
 * call means the person is not offered an action that can only fail.
 */
export function artifactSource(artifact: Artifact): string | null {
  const stored = artifact.metadata?.huggingface_id
  if (typeof stored === "string" && stored.includes("/"))
    return `hf://${stored}`
  for (const location of artifact.locations) {
    if (/^hf:/i.test(location.uri))
      return `hf://${location.uri.replace(/^hf:\/\/|^hf:/i, "").split("@")[0]}`
  }
  return null
}

/**
 * What a refresh moved, in a line a person can read.
 *
 * The plane returns dotted field names (`metadata.shape`), which are the honest
 * answer and not a sentence. The `metadata.` prefix carries no information at this
 * point — everything derived from a config lives there — so it is dropped.
 */
export function refreshSummary(changed: string[]): string {
  if (changed.length === 0)
    return "the source says what was already recorded — nothing changed"
  const names = changed.map((field) => field.replace(/^metadata\./, ""))
  return `updated ${names.join(", ")}`
}

/** `tp8/pp1/ep1`, the shorthand every engine log prints. */
export function parallelismLabel(p: {
  tensor: number
  pipeline: number
  expert: number
}): string {
  return `tp${p.tensor}/pp${p.pipeline}/ep${p.expert}`
}

/**
 * The steps a deployment passes through between being declared and serving, and
 * which one an observed `(phase, reason)` places it at.
 *
 * The reasons are the operator's closed set (`MESSAGES.md`). They are mapped here
 * rather than rendered raw because a person watching a deployment come up wants to
 * know how far along it is, and `WeightsNotResident` answers that only if you
 * already know what the operator does in what order.
 */
export const LIFECYCLE_STEPS = [
  { key: "declared", label: "Declared" },
  { key: "accepted", label: "Accepted" },
  { key: "dependencies", label: "Weights" },
  { key: "capacity", label: "Capacity" },
  { key: "rendering", label: "Rendering" },
  { key: "applied", label: "Applied" },
  { key: "serving", label: "Serving" },
] as const

export type LifecycleStep = (typeof LIFECYCLE_STEPS)[number]["key"]

const REASON_STEPS: Record<string, LifecycleStep> = {
  Accepted: "accepted",
  ArtifactNotDeclared: "dependencies",
  WeightsNotResident: "dependencies",
  WarmSetIncomplete: "dependencies",
  NoSchedulableCapacity: "capacity",
  Rendering: "rendering",
  Applied: "applied",
  ComponentNotReady: "applied",
  Progressing: "serving",
  Serving: "serving",
  PayloadUndecodable: "declared",
  RenderFailed: "rendering",
  NoClusterWork: "declared",
}

/**
 * Where a phase alone puts the strip, for a `reason` this console has never heard of.
 *
 * The fallback is the phase and not `declared`. A cluster runs its own build and may
 * report a sub-state that postdates this console, and answering "declared" for it
 * would walk a serving deployment back to the first step — worse than the raw name,
 * which is at least true. The phase is the field the contract says to compute on.
 */
const PHASE_STEPS: Record<string, LifecycleStep> = {
  pending: "accepted",
  admitted: "accepted",
  placed: "applied",
  degraded: "serving",
  running: "serving",
  terminating: "serving",
  failed: "declared",
}

/**
 * Both halves, because neither answers alone.
 *
 * `reason` is the precise one and is preferred, but it goes stale in one direction the
 * phase does not: everything the operator reports after applying used to carry
 * `Applied`, so a reason-only reading pinned a serving deployment to "Applied" while
 * the counts underneath it said otherwise.
 */
export function lifecycleStepFor(
  phase: string,
  reason: string | null | undefined
): LifecycleStep {
  const byReason = reason ? REASON_STEPS[reason] : undefined
  return byReason ?? PHASE_STEPS[phase] ?? "declared"
}

/**
 * How a `reason` reads to somebody who has not read the operator's source.
 *
 * Falls back to the reason itself, which is deliberately not "Unknown": the raw
 * name is what they will search the operator logs for.
 */
const REASON_TEXT: Record<string, string> = {
  Accepted: "The cluster has the instruction and is working on it",
  ArtifactNotDeclared: "Waiting for the artifact to reach the cluster",
  WeightsNotResident: "Downloading the weights onto the cluster",
  WarmSetIncomplete: "Weights are still being fetched",
  NoSchedulableCapacity: "No node has enough accelerators free",
  Rendering: "Building the objects that will run this",
  Applied: "Applied to the cluster; waiting for the first replica",
  ComponentNotReady: "Applied; the cluster is still bringing the replicas up",
  Progressing: "Replicas are still coming up",
  Serving: "Every replica is ready",
  PayloadUndecodable: "The cluster could not read this deployment",
  RenderFailed: "The cluster could not turn this into anything runnable",
  NoClusterWork: "Held as declared state — authority is observed",
  PodUnschedulable: "The cluster cannot schedule a replica",
  CrashLooping: "A replica is repeatedly crashing",
  ContainerConfigInvalid: "A replica has invalid container configuration",
  ImagePullFailed: "The cluster could not pull an image",
  ClusterRejected:
    "The cluster refused these objects, and will refuse them again until the workload changes",
  NoReplicasStarted: "No replica started, and nothing said why",
  FingerprintMismatch: "The running workload no longer matches its declaration",
  LiveObjectUnreadable: "The cluster could not inspect the running workload",
}

export function reasonText(reason: string): string {
  return REASON_TEXT[reason] ?? reason
}

/**
 * The closed set a workload or deployment reads as in the console.
 *
 * Derived here from `admission_phase`, the observed phase, `reason` and the
 * replica counts, because no single field on the wire answers "what is it doing
 * now". Ordered by how loudly each one should win a rollup: a workload with one
 * failed cluster and one serving cluster reads `failed`.
 */
export const LIFECYCLE_STATES = [
  "failed",
  "blocked",
  "degraded",
  "awaiting capacity",
  "pending",
  "downloading",
  "starting",
  "stopping",
  "paused",
  "running",
] as const

export type LifecycleState = (typeof LIFECYCLE_STATES)[number]

const LIFECYCLE_TONES: Record<LifecycleState, StatusTone> = {
  failed: "danger",
  blocked: "warn",
  degraded: "warn",
  "awaiting capacity": "warn",
  pending: "neutral",
  downloading: "info",
  starting: "info",
  stopping: "neutral",
  paused: "warn",
  running: "ok",
}

const ATTENTION: ReadonlySet<LifecycleState> = new Set([
  "failed",
  "blocked",
  "degraded",
])

const DOWNLOAD_REASONS = new Set([
  "ArtifactNotDeclared",
  "WeightsNotResident",
  "WarmSetIncomplete",
])
const CAPACITY_REASONS = new Set(["NoSchedulableCapacity", "PodUnschedulable"])

export interface OperationalStatus {
  phase: LifecycleState
  tone: StatusTone
  reason: string | null
  placement: string
  needsAttention: boolean
}

function operational(
  phase: LifecycleState,
  reason: string | null,
  placement: string
): OperationalStatus {
  return {
    phase,
    tone: LIFECYCLE_TONES[phase],
    reason,
    placement,
    needsAttention: ATTENTION.has(phase),
  }
}

/**
 * Where one deployment is in its lifecycle.
 *
 * Only an observed `failed` is a failure. The operator sends `last_error_code`
 * for waits that clear on their own too — an unschedulable pod, a crash loop —
 * and reading every error code as fatal showed deployments that were still
 * loading their weights as failed.
 *
 * Zero ready replicas with no complaint is `starting`, whatever the per-role
 * counts say: every role reads absent before its first worker has loaded, and
 * that is not the missing-role fault `degraded` is for.
 */
export function deploymentOperationalStatus(
  deployment: WorkloadDeployment
): OperationalStatus {
  const observed = deployment.status
  const placement = deployment.placed_at ? "placed" : deployment.admission_phase
  const code = observed?.last_error_code ?? null
  const reason = observed?.reason ?? null
  const text = (key: string | null) => (key ? reasonText(key) : null)

  if (observed?.phase === "failed" || deployment.admission_phase === "failed") {
    return operational(
      "failed",
      text(code ?? reason ?? deployment.admission_reason ?? "failed"),
      placement
    )
  }
  if (!observed) {
    if (deployment.admission_phase === "pending") {
      return operational("pending", deployment.admission_reason ?? null, placement)
    }
    return operational("starting", null, placement)
  }
  if (observed.phase === "terminating") {
    return operational("stopping", null, placement)
  }
  if (reason && CAPACITY_REASONS.has(reason)) {
    return operational("awaiting capacity", text(reason), placement)
  }
  if (reason && DOWNLOAD_REASONS.has(reason)) {
    return operational("downloading", text(reason), placement)
  }

  const roles = roleReadiness(observed)
  const ready = roles
    ? roles.reduce((sum, role) => sum + role.ready, 0)
    : observed.ready_replicas
  if (ready === 0) {
    if (code) return operational("blocked", text(code), placement)
    if (observed.phase === "pending" || observed.phase === "admitted") {
      return operational("pending", text(reason), placement)
    }
    return operational("starting", text(reason), placement)
  }

  const roleMissing = roles?.find((role) => role.absent) ?? null
  if (roleMissing) {
    return operational("degraded", `no ${roleMissing.role} worker`, placement)
  }
  if (
    observed.phase === "degraded" ||
    (roles !== null && !rolesReady(roles)) ||
    observed.ready_replicas < observed.desired_replicas
  ) {
    return operational("degraded", text(code ?? reason), placement)
  }
  return operational("running", null, placement)
}

/** The loudest per-cluster lifecycle state on a global workload. */
export function workloadOperationalStatus(
  workload: Workload
): OperationalStatus {
  const placement =
    workload.scale.placed >= workload.scale.desired
      ? "placed"
      : `${workload.scale.placed}/${workload.scale.desired} placed`
  if (workload.paused) return operational("paused", null, placement)
  if (workload.deployments.length === 0) {
    return operational(
      workload.phase === "failed" ? "failed" : "pending",
      null,
      placement
    )
  }
  const loudest = workload.deployments
    .map(deploymentOperationalStatus)
    .reduce((a, b) =>
      LIFECYCLE_STATES.indexOf(b.phase) < LIFECYCLE_STATES.indexOf(a.phase)
        ? b
        : a
    )
  return { ...loudest, placement }
}

/**
 * The engine argument each typed `serving` field speaks for, keyed without the
 * leading dashes — the same table the plane renders from.
 *
 * Only the fields that genuinely map to one flag are here. An engine this
 * console has never heard of maps nothing, and neither `context_overflow` nor
 * EOS suppression is a single flag on every engine.
 *
 * The vLLM rows are the flags of the program the cluster actually runs, which is
 * `python3 -m dynamo.vllm` and not `vllm serve`. `--tool-call-parser` and
 * `--guided-decoding-backend` are the standalone server's and exist on neither
 * Dynamo entrypoint, so previewing them here promised a configuration the plane
 * will not render and the worker would not start under.
 */
export const SERVING_FLAGS: Record<string, Record<string, string>> = {
  vllm: {
    parser: "reasoning-parser",
    max_images_per_request: "limit-mm-per-prompt",
    max_videos_per_request: "limit-mm-per-prompt",
  },
  sglang: {
    parser: "reasoning-parser",
    tool_call_parser: "tool-call-parser",
    structured_output_backend: "grammar-backend",
  },
}

/**
 * Valueless flags a typed field cannot work without, per engine — the same table
 * the plane renders from.
 *
 * `--enable-multimodal` is what turns the multimodal components on at all, so a
 * per-modality cap without it caps a path that is switched off. It is one switch
 * for every modality, which is why both caps name it.
 *
 * These fill a gap rather than win one, which is the opposite of how a typed field
 * behaves: they are inferred and not declared, so an `engine_args` that spells one
 * out by hand keeps what it wrote.
 */
export const SERVING_COMPANION_FLAGS: Record<
  string,
  Record<string, string[]>
> = {
  vllm: {
    max_images_per_request: ["enable-multimodal"],
    max_videos_per_request: ["enable-multimodal"],
  },
}

/** How a typed serving field reads back to somebody looking at engine args. */
const SERVING_FIELD_LABELS: Record<string, string> = {
  parser: "serving.reasoning.parser",
  tool_call_parser: "serving.tool_call_parser",
  structured_output_backend: "serving.structured_output_backend",
  max_images_per_request: "serving.max_images_per_request",
  max_videos_per_request: "serving.max_videos_per_request",
}

/**
 * The per-modality caps, and the key each one holds inside `--limit-mm-per-prompt`
 * — the same table the plane renders from.
 *
 * Two typed fields and one flag: the engine takes the whole map at once, so a
 * workload that caps both renders `{"image": 4, "video": 1}` and never two flags.
 */
export const MODALITY_LIMITS: Record<string, string> = {
  max_images_per_request: "image",
  max_videos_per_request: "video",
}

export interface ServingOverride {
  /** The engine flag both spellings set, keyed without its leading dashes. */
  flag: string
  /** The typed field that wins it. */
  field: string
  /** What `engine_args` asks for, and what the plane will not render. */
  ignored: string
  /** What the typed field renders instead. */
  effective: string
}

/** The flags the typed serving block speaks for, on this engine. */
export function servingArgs(
  engine: string,
  serving: ServingBehaviour
): Record<string, string> {
  const flags = SERVING_FLAGS[engine.trim().toLowerCase()] ?? {}
  const args: Record<string, string> = {}
  const typed: [string, string | null][] = [
    ["parser", serving.reasoning.parser],
    ["tool_call_parser", serving.tool_call_parser],
    ["structured_output_backend", serving.structured_output_backend],
  ]
  for (const [field, value] of typed) {
    if (value && flags[field]) args[flags[field]] = value
  }
  const limits = new Map<string, Record<string, number>>()
  for (const [field, modality] of Object.entries(MODALITY_LIMITS)) {
    const limit = serving[field as keyof ServingBehaviour] as number | null
    if (limit === null || limit === undefined || !flags[field]) continue
    const map = limits.get(flags[field]) ?? {}
    map[modality] = limit
    limits.set(flags[field], map)
  }
  for (const [flag, map] of limits) {
    // Spelled out rather than `JSON.stringify`d, because this has to be the same
    // bytes the plane renders: Python's `json.dumps` puts a space after the colon
    // and after the comma, and a preview that differs from what will be sent reads
    // as a pending change.
    const body = Object.entries(map)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([modality, limit]) => `"${modality}": ${limit}`)
      .join(", ")
    args[flag] = `{${body}}`
  }
  return args
}

/**
 * The collisions between hand-written `engine_args` and the typed block, so that
 * a form can say which value will actually take effect.
 *
 * The typed field wins, because it is the one the plane can validate, display
 * and verify. `engine_args` remains the escape hatch for a flag nothing models —
 * but a flag that *is* modelled has one declared home, and silently dropping the
 * other spelling is how somebody ends up debugging a value they can see.
 */
export function servingOverrides(
  engine: string,
  engineArgs: Record<string, unknown>,
  serving: ServingBehaviour
): ServingOverride[] {
  const flags = SERVING_FLAGS[engine.trim().toLowerCase()] ?? {}
  const derived = servingArgs(engine, serving)
  const declared = new Map<string, string>()
  for (const [key, value] of Object.entries(engineArgs ?? {})) {
    declared.set(String(key).replace(/^--/, ""), String(value))
  }
  const out: ServingOverride[] = []
  const reported = new Set<string>()
  for (const [field, flag] of Object.entries(flags)) {
    const ignored = declared.get(flag)
    const effective = derived[flag]
    if (ignored === undefined || effective === undefined) continue
    if (ignored === effective) continue
    // Two caps name one flag, and the collision is with the flag rather than
    // with either field. Reporting it twice would read as two conflicts.
    if (reported.has(flag)) continue
    reported.add(flag)
    out.push({
      flag,
      field: SERVING_FIELD_LABELS[field] ?? field,
      ignored,
      effective,
    })
  }
  return out
}

/** The valueless flags the typed serving block implies, on this engine. */
export function servingCompanionArgs(
  engine: string,
  serving: ServingBehaviour
): Record<string, string> {
  const implied = SERVING_COMPANION_FLAGS[engine.trim().toLowerCase()] ?? {}
  const args: Record<string, string> = {}
  for (const field of Object.keys(MODALITY_LIMITS)) {
    const limit = serving[field as keyof ServingBehaviour] as number | null
    if (limit === null || limit === undefined) continue
    for (const flag of implied[field] ?? []) args[flag] = ""
  }
  return args
}

/**
 * `engine_args` with the typed serving flags laid over the top. The plane sends
 * this as `resolved_engine_args` on every read; this is the same arithmetic for
 * a form that has not saved yet.
 *
 * Companion flags go underneath rather than over, so an `engine_args` that names
 * one keeps its own value.
 */
export function resolvedEngineArgs(
  engine: string,
  engineArgs: Record<string, unknown>,
  serving: ServingBehaviour
): Record<string, string> {
  const resolved: Record<string, string> = {
    ...servingCompanionArgs(engine, serving),
  }
  for (const [key, value] of Object.entries(engineArgs ?? {})) {
    resolved[String(key).replace(/^--/, "")] = String(value)
  }
  return { ...resolved, ...servingArgs(engine, serving) }
}

/** Whether anything about reasoning was declared rather than left at its default. */
export function reasoningDeclared(reasoning: ServingReasoning): boolean {
  return Boolean(
    reasoning.default_on ||
    reasoning.parser ||
    reasoning.transport === "inline_tags" ||
    reasoning.think_begin_token ||
    reasoning.think_end_token ||
    reasoning.suppress_eos_until_think_end
  )
}

/**
 * How much of a declared thing is actually honoured, and by what.
 *
 * `obeyed` is rendered to the cluster or acted on by the gateway. `asserted` is
 * a claim nothing renders, falsifiable only by a conformance run. `disclosure`
 * is documented and enforced by nothing, in any configuration. `conditional` is
 * a term only while the gateway runs with its `enforce` capability on.
 */
export type Enforcement = "obeyed" | "asserted" | "disclosure" | "conditional"

export interface SurfaceField {
  enforcement: Enforcement
  /** What honours it, or what it means where nothing does. */
  note: string
}

/**
 * Field by field, what the gateway does with an endpoint's surface.
 *
 * Four of the eight are terms and four are not, and the split is not cosmetic:
 * three of the disclosures are structural and no capability changes them. A
 * console that drew all eight the same way would let an operator promise a
 * customer a per-endpoint request ceiling that nothing applies.
 */
export const SURFACE_ENFORCEMENT: Record<keyof EndpointSurface, SurfaceField> =
  {
    protocols: {
      enforcement: "disclosure",
      note: "The gateway routes on the model in the body and never branches on the path, so nothing checks this. A conformance run verifies it.",
    },
    auth_mode: {
      enforcement: "disclosure",
      note: "A fact about the system in front of us, under every capability. The gateway's identity mode is process-wide and cannot express a per-endpoint one.",
    },
    error_shape: {
      enforcement: "disclosure",
      note: "The gateway never reads an upstream response body. `passthrough` is the only value: you get what the engine said.",
    },
    max_request_bytes: {
      enforcement: "disclosure",
      note: "Advisory. The body is read before the endpoint is known, so the applied ceiling is the gateway's process-wide 32 MiB.",
    },
    request_timeout_seconds: {
      enforcement: "obeyed",
      note: "A per-route deadline on the proxied request. Null leaves the transport's own. Separate from a serving quota's queue timeout; the smaller wins.",
    },
    streaming_transport: {
      enforcement: "obeyed",
      note: "Whether the gateway passes SSE through or buffers to one response. Not the same claim as the workload's `streaming_supported`.",
    },
    cors_enabled: {
      enforcement: "obeyed",
      note: "Whether the exposure answers a preflight.",
    },
    output_length_params: {
      enforcement: "conditional",
      note: "Read with the serving quota's `max_output_tokens` on every request that carries a length field — but only refused while the gateway runs with `enforce`.",
    },
    policy: {
      enforcement: "obeyed",
      note: "The request terms this endpoint sells. The gateway reads them off the resolved table and applies every rule whose scope matches, enforcing what it understands and counting what it does not.",
    },
  }

/**
 * The scope a rule was measured on, as one line.
 *
 * An empty scope is not a gap: an unscoped rule applies to every route and both
 * transport modes, which is exactly what a v1 policy meant. Saying "any" says so
 * rather than leaving a blank an operator reads as missing data.
 */
export function policyRuleScope(rule: PolicyRule): string {
  const parts: string[] = [rule.route || "any route"]
  if (rule.stream === true) parts.push("streaming")
  if (rule.stream === false) parts.push("non-streaming")
  if (rule.when_present) parts.push(`when ${rule.when_present} is sent`)
  return parts.join(" · ")
}

/**
 * What one rule says, in the order the terms are read.
 *
 * `deny` refuses, the bounds constrain, and `default`, `drop` and `value_from`
 * rewrite and never fail a request — so a rule that only rewrites reads as a
 * rewrite here and never as a refusal.
 */
export function policyRuleTerms(rule: PolicyRule): string[] {
  const terms: string[] = []
  if (rule.deny) terms.push("denied")
  if (rule.enum && rule.enum.length > 0)
    terms.push(`one of ${rule.enum.map((v) => JSON.stringify(v)).join(", ")}`)
  if (rule.min != null) terms.push(`≥ ${rule.min}`)
  if (rule.max != null) terms.push(`≤ ${rule.max}`)
  if (rule.default !== undefined && rule.default !== null)
    terms.push(`defaults to ${JSON.stringify(rule.default)}`)
  if (rule.drop) terms.push("dropped from the request")
  if (rule.value_from) terms.push(`copied from ${rule.value_from}`)
  return terms
}

/**
 * The acceptance floor a verdict applies to a declared baseline.
 *
 * A property of the criteria version and not of the row: burying it in
 * `tolerance_pct` would let two artifacts be graded against two silently
 * different standards, and raising the standard would mean rewriting every
 * baseline ever recorded.
 */
export const ACCEPTANCE_FLOOR = 0.85

export function acceptanceFloor(value: number): number {
  return value * ACCEPTANCE_FLOOR
}

/** Trims trailing zeros so 0.85 x 74.1 reads as 62.985 rather than 62.985000. */
export function formatScore(value: number): string {
  return String(Number(value.toFixed(4)))
}

/**
 * The blocks a serving configuration that says nothing carries. Every default is
 * the behaviour of an undeclared block, so filling a form with these changes
 * nothing.
 */
export function emptyServing(): ServingBehaviour {
  return {
    reasoning: {
      default_on: false,
      parser: null,
      transport: "reasoning_content",
      think_begin_token: null,
      think_end_token: null,
      suppress_eos_until_think_end: false,
    },
    stream_usage: "final_chunk",
    context_overflow: "reject",
    structured_output_backend: null,
    tool_call_parser: null,
    max_images_per_request: null,
    max_videos_per_request: null,
  }
}

export function emptyCapabilities(): ServingCapabilities {
  return {
    reasoning_supported: false,
    streaming_supported: true,
    structured_output_modes: ["text"],
    tool_calling_supported: false,
    tool_choice_modes: [],
    parallel_tool_calls: false,
    input_modalities: ["text"],
    prompt_cache_mode: "off",
    prompt_cache_ttl_seconds: null,
    asserted_by: null,
    asserted_at: null,
    source_uri: null,
  }
}

/** The surface an endpoint that says nothing is exposed on. */
export function emptySurface(): EndpointSurface {
  return {
    protocols: ["openai.chat"],
    auth_mode: "none",
    error_shape: "passthrough",
    max_request_bytes: null,
    request_timeout_seconds: null,
    streaming_transport: "sse",
    cors_enabled: false,
    output_length_params: ["max_tokens"],
  }
}

/** An upstream block with nothing claimed and nobody having claimed it. */
export function emptyUpstream(): ArtifactUpstream {
  return {
    provider: null,
    provider_model_id: null,
    context_length: null,
    max_output_tokens: null,
    input_modalities: ["text"],
    reasoning: null,
    tool_calling: null,
    structured_output_modes: [],
    licence: null,
    released_at: null,
    source_uri: null,
    recorded_by: null,
    recorded_at: null,
  }
}

/**
 * Whether the publisher's claim says anything at all.
 *
 * `input_modalities` defaulting to `["text"]` is not a claim — it is the shape
 * of an empty block — so it does not count, and neither does an empty modes
 * list. Without this a console would demand provenance for a block nobody
 * touched.
 */
export function upstreamClaimed(upstream: ArtifactUpstream): boolean {
  return Boolean(
    upstream.provider ||
    upstream.provider_model_id ||
    upstream.context_length !== null ||
    upstream.max_output_tokens !== null ||
    upstream.reasoning !== null ||
    upstream.tool_calling !== null ||
    upstream.structured_output_modes.length > 0 ||
    upstream.licence ||
    upstream.released_at
  )
}
