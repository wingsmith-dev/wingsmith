import type { StatusTone } from "@/components/layout/status-dot"
import type {
  ArtifactUpstream,
  EndpointSurface,
  RecipeCapabilities,
  RecipeReasoning,
  RecipeServing,
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

const RECIPE_LIFECYCLE_TONES: Record<string, StatusTone> = {
  published: "ok",
  draft: "neutral",
  deprecated: "warn",
}

/** The tone for a recipe's lifecycle: published | draft | deprecated. */
export function recipeLifecycleTone(lifecycle: string): StatusTone {
  return RECIPE_LIFECYCLE_TONES[lifecycle] ?? "neutral"
}

export const RECIPE_LIFECYCLES = ["published", "draft", "deprecated"] as const

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
  { key: "dependencies", label: "Recipe & weights" },
  { key: "capacity", label: "Capacity" },
  { key: "rendering", label: "Rendering" },
  { key: "applied", label: "Applied" },
  { key: "serving", label: "Serving" },
] as const

export type LifecycleStep = (typeof LIFECYCLE_STEPS)[number]["key"]

const REASON_STEPS: Record<string, LifecycleStep> = {
  Accepted: "accepted",
  RecipeNotDeclared: "dependencies",
  ArtifactNotDeclared: "dependencies",
  WeightsNotResident: "dependencies",
  WarmSetIncomplete: "dependencies",
  NoSchedulableCapacity: "capacity",
  Rendering: "rendering",
  Applied: "applied",
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
  RecipeNotDeclared: "Waiting for the recipe to reach the cluster",
  ArtifactNotDeclared: "Waiting for the artifact to reach the cluster",
  WeightsNotResident: "Downloading the weights onto the cluster",
  WarmSetIncomplete: "Weights are still being fetched",
  NoSchedulableCapacity: "No node has enough accelerators free",
  Rendering: "Building the objects that will run this",
  Applied: "Applied to the cluster; waiting for the first replica",
  Progressing: "Replicas are still coming up",
  Serving: "Every replica is ready",
  PayloadUndecodable: "The cluster could not read this deployment",
  RenderFailed: "The cluster could not turn this into anything runnable",
  NoClusterWork: "Held as declared state — authority is observed",
}

export function reasonText(reason: string): string {
  return REASON_TEXT[reason] ?? reason
}

/**
 * The engine argument each typed `serving` field speaks for, keyed without the
 * leading dashes — the same table the plane renders from.
 *
 * Only the fields that genuinely map to one flag are here. An engine this
 * console has never heard of maps nothing, and neither `context_overflow` nor
 * EOS suppression is a single flag on every engine.
 */
export const SERVING_FLAGS: Record<string, Record<string, string>> = {
  vllm: {
    parser: "reasoning-parser",
    tool_call_parser: "tool-call-parser",
    structured_output_backend: "guided-decoding-backend",
    max_images_per_request: "limit-mm-per-prompt",
  },
  sglang: {
    parser: "reasoning-parser",
    tool_call_parser: "tool-call-parser",
    structured_output_backend: "grammar-backend",
  },
}

/** How a typed serving field reads back to somebody looking at engine args. */
const SERVING_FIELD_LABELS: Record<string, string> = {
  parser: "serving.reasoning.parser",
  tool_call_parser: "serving.tool_call_parser",
  structured_output_backend: "serving.structured_output_backend",
  max_images_per_request: "serving.max_images_per_request",
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
  serving: RecipeServing
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
  if (serving.max_images_per_request !== null && flags.max_images_per_request) {
    args[flags.max_images_per_request] =
      `image=${serving.max_images_per_request}`
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
  serving: RecipeServing
): ServingOverride[] {
  const flags = SERVING_FLAGS[engine.trim().toLowerCase()] ?? {}
  const derived = servingArgs(engine, serving)
  const declared = new Map<string, string>()
  for (const [key, value] of Object.entries(engineArgs ?? {})) {
    declared.set(String(key).replace(/^--/, ""), String(value))
  }
  const out: ServingOverride[] = []
  for (const [field, flag] of Object.entries(flags)) {
    const ignored = declared.get(flag)
    const effective = derived[flag]
    if (ignored === undefined || effective === undefined) continue
    if (ignored === effective) continue
    out.push({
      flag,
      field: SERVING_FIELD_LABELS[field] ?? field,
      ignored,
      effective,
    })
  }
  return out
}

/**
 * `engine_args` with the typed serving flags laid over the top. The plane sends
 * this as `resolved_engine_args` on every read; this is the same arithmetic for
 * a form that has not saved yet.
 */
export function resolvedEngineArgs(
  engine: string,
  engineArgs: Record<string, unknown>,
  serving: RecipeServing
): Record<string, string> {
  const resolved: Record<string, string> = {}
  for (const [key, value] of Object.entries(engineArgs ?? {})) {
    resolved[String(key).replace(/^--/, "")] = String(value)
  }
  return { ...resolved, ...servingArgs(engine, serving) }
}

/** Whether anything about reasoning was declared rather than left at its default. */
export function reasoningDeclared(reasoning: RecipeReasoning): boolean {
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
      note: "Whether the gateway passes SSE through or buffers to one response. Not the same claim as the recipe's `streaming_supported`.",
    },
    cors_enabled: {
      enforcement: "obeyed",
      note: "Whether the exposure answers a preflight.",
    },
    output_length_params: {
      enforcement: "conditional",
      note: "Read with the serving quota's `max_output_tokens` on every request that carries a length field — but only refused while the gateway runs with `enforce`.",
    },
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
 * The blocks a recipe that says nothing carries. Every default is the behaviour
 * of an undeclared recipe, so filling a form with these changes nothing.
 */
export function emptyServing(): RecipeServing {
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
  }
}

export function emptyCapabilities(): RecipeCapabilities {
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
