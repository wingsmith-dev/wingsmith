/**
 * Deriving a serving block from an artifact and a fitment row, so that nobody types one.
 *
 * The serving block asks twenty-five questions. Six of them the fitment already answered
 * from the weights, and most of the rest are either derivable or a default that only
 * an expert should ever move. What is left once those are taken out is a name, and a
 * name is not a question worth asking either — so this module answers all of them and
 * the deploy flow asks none.
 *
 * Nothing here is authority. The plane sizes the fit and places the workload; this
 * only assembles the request those two answers imply. Where a field cannot be derived
 * honestly it is left off the request entirely rather than guessed, so the plane's own
 * default applies instead of one invented here.
 */

import { emptyServing } from "./display"
import type {
  Artifact,
  ServingBehaviour,
  ServingDefaults,
  ServingFootprint,
  UpstreamServing,
  Workload,
  WorkloadServing,
  WorkloadServingRequest,
} from "./types"

/** The engine a derived serving block is built for until something offers a choice. */
export const DEFAULT_ENGINE = "vllm"

/**
 * Engine version is required by `WorkloadServingRequest` and is not derivable from an
 * artifact — the console does not know what builds exist. `latest` is the honest
 * stand-in: it says "whatever the operator ships" rather than pinning a version this
 * code invented, and it is visible and editable on the workload afterwards.
 */
export const DEFAULT_ENGINE_VERSION = "latest"

/** `Llama-3.3-70B-Instruct` -> `llama-3-3-70b-instruct`. */
export function slugify(value: string): string {
  return value
    .split("/")
    .pop()!
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
}

/** `32768` -> `32k`, `8192` -> `8k`, `1000` -> `1000`. */
export function shortTokens(value: number): string {
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024}k`
  return String(value)
}

/**
 * The name a workload gets when nobody typed one.
 *
 * The artifact rather than the fit, because a workload is the thing you asked to
 * run and you asked for the weights — the fit is how, not what. Collisions are the
 * plane's to reject; this only avoids the ones it can see, by suffixing past names
 * already in the list.
 */
export function derivedWorkloadName(
  artifact: Artifact,
  taken: string[] = []
): string {
  const base = slugify(artifact.name)
  if (!taken.includes(base)) return base
  for (let n = 2; n < 1000; n++) {
    const candidate = `${base}-${n}`
    if (!taken.includes(candidate)) return candidate
  }
  return base
}

/**
 * Whether a replica of this footprint has to stay inside one fabric domain.
 *
 * Derived, not asked. A tensor group reaching over the backend fabric is the one
 * configuration that is always wrong, so any footprint whose tensor degree exceeds
 * what a single node holds requires a domain. When the fit is pipeline-parallel the
 * tensor groups are already node-sized and the pipeline stages are what cross the
 * fabric, which is exactly what a fabric is for.
 */
export function requiresSingleDomain(footprint: ServingFootprint): boolean {
  return footprint.pipeline_parallel <= 1 && footprint.tensor_parallel > 1
}

/**
 * What the plane proposed for these weights, and who proposed it.
 *
 * Two sources and they are never merged into one number. `defaults` is the plane's
 * own arithmetic over `config.json` — what the weights need. `upstream` is a third
 * party's measurement of a different question — what a configuration did when they
 * served it — and every field of it is a claim with a permalink behind it. The
 * console seeds from both and says which one a value came from, because "vLLM
 * verified this on H200" is the most useful thing on the form and "we divided a
 * memory figure by a card" is a different kind of statement.
 */
export interface ServingProposal {
  defaults?: ServingDefaults | null
  upstream?: UpstreamServing | null
}

/**
 * Which fields the proposal actually answered, for a form that has to attribute
 * every value it did not derive itself.
 */
export type SeededFrom = "derived" | "weights" | "upstream"

/**
 * The typed behaviour block a corpus entry implies.
 *
 * Only the parsers, and only when the corpus names one: everything else in the
 * block is a rendered flag nobody upstream has an opinion about, and a default
 * invented here would be indistinguishable from a tested one on the form.
 */
export function proposedBehaviour(
  upstream?: UpstreamServing | null
): ServingBehaviour {
  const block = emptyServing()
  if (!upstream) return block
  return {
    ...block,
    reasoning: { ...block.reasoning, parser: upstream.reasoning_parser },
    tool_call_parser: upstream.tool_call_parser,
  }
}

/**
 * The serving block that a fitment row plus an artifact imply.
 *
 * The fit answers the six things it is — which card, how many, how they are split,
 * at what window and precision — and everything else comes from the proposal or is
 * left at the plane's default. Left alone matters: `max_batch_tokens`,
 * `kv_cache_fraction`, `resources` and `topology_domain_kind` are never guessed,
 * because neither the console nor the corpus has an honest value and the plane's
 * own default beats a number invented here.
 *
 * `prefix_cache_enabled` is the one opinion: on. It is free on a repeated prompt and
 * the engines default it on themselves.
 */
export function derivedServingRequest(
  artifact: Artifact,
  footprint: ServingFootprint,
  proposal: ServingProposal = {},
  overrides: { engine?: string; engine_version?: string } = {}
): WorkloadServingRequest {
  const { defaults, upstream } = proposal
  return {
    artifact: artifact.id,
    engine: overrides.engine || upstream?.engine || DEFAULT_ENGINE,
    engine_version:
      overrides.engine_version ||
      upstream?.engine_version_min ||
      DEFAULT_ENGINE_VERSION,
    context_length: footprint.context_length,
    shape: {
      device_class: footprint.device_class,
      device_count: footprint.device_count,
      device_memory_mib_min: defaults?.device_memory_mib_min ?? 0,
      requires_single_topology_domain:
        upstream?.requires_single_topology_domain ||
        requiresSingleDomain(footprint),
      topology_domain_kind: null,
    },
    parallelism: {
      tensor: footprint.tensor_parallel,
      pipeline: footprint.pipeline_parallel,
      expert: 1,
    },
    quantization: footprint.quantization || null,
    dtype: defaults?.dtype ?? null,
    cache: {
      kv_cache_dtype: upstream?.kv_cache_dtype ?? null,
      kv_cache_fraction: null,
      prefix_cache_enabled: true,
      prefix_cache_config: {},
    },
    serving: proposedBehaviour(upstream),
  }
}

/**
 * Where each seeded field came from, for the form to attribute beside it.
 *
 * Attribution is the whole point of seeding: a value nobody can trace is a value
 * nobody will move, and a form full of those is how an operator ends up running a
 * configuration they never chose and cannot defend.
 */
export function servingProvenance(
  proposal: ServingProposal = {}
): Record<string, SeededFrom> {
  const { defaults, upstream } = proposal
  const at: Record<string, SeededFrom> = {
    device_class: "derived",
    device_count: "derived",
    tensor_parallel: "derived",
    pipeline_parallel: "derived",
    context_length: "derived",
    quantization: "derived",
  }
  if (defaults?.dtype) at.dtype = "weights"
  if (defaults?.device_memory_mib_min) at.device_memory_mib_min = "weights"
  if (upstream?.engine) at.engine = "upstream"
  if (upstream?.engine_version_min) at.engine_version = "upstream"
  if (upstream?.kv_cache_dtype) at.kv_cache_dtype = "upstream"
  if (upstream?.reasoning_parser) at.reasoning_parser = "upstream"
  if (upstream?.tool_call_parser) at.tool_call_parser = "upstream"
  if (upstream?.requires_single_topology_domain)
    at.requires_single_topology_domain = "upstream"
  return at
}

/**
 * Workloads already running exactly this configuration.
 *
 * Informational and never a fork. The plane's answer is `serving_digest` — one
 * hash over everything that reaches an engine — but a digest is the plane's to
 * compute and there is nothing to compute it over until the workload exists, so
 * this compares the fields the flow actually sets and says what it found. It
 * decides nothing: creating a second workload on one configuration is a normal
 * thing to want, and the two will carry the same digest either way.
 *
 * The old fork asked a person to resolve this and got it wrong whenever a field
 * outside its comparison had been tuned by hand. This asks nothing.
 */
export function workloadsMatching(
  workloads: Workload[],
  artifact: Artifact,
  footprint: ServingFootprint,
  engine: string = DEFAULT_ENGINE
): Workload[] {
  return workloads.filter(
    (workload) =>
      workload.serving.artifact_id === artifact.id &&
      workload.serving.engine === engine &&
      workload.serving.shape.device_class === footprint.device_class &&
      workload.serving.shape.device_count === footprint.device_count &&
      workload.serving.context_length === footprint.context_length &&
      workload.serving.parallelism.tensor === footprint.tensor_parallel &&
      workload.serving.parallelism.pipeline === footprint.pipeline_parallel &&
      (workload.serving.quantization ?? "") === (footprint.quantization ?? "")
  )
}

/** `2 x H100 · tp2 · 32k · fp8` — a footprint on one line, for a collapsed step. */
export function footprintSummary(
  footprint: ServingFootprint,
  label?: string
): string {
  const parts = [
    `${footprint.device_count} x ${label ?? footprint.device_class}`,
    `tp${footprint.tensor_parallel}`,
  ]
  if (footprint.pipeline_parallel > 1)
    parts.push(`pp${footprint.pipeline_parallel}`)
  if (footprint.context_length > 0)
    parts.push(shortTokens(footprint.context_length))
  if (footprint.quantization) parts.push(footprint.quantization)
  return parts.join(" · ")
}

/** The footprint a workload's serving block already describes. */
export function footprintOfServing(serving: WorkloadServing): ServingFootprint {
  return {
    device_class: serving.shape.device_class,
    device_count: serving.shape.device_count,
    tensor_parallel: serving.parallelism.tensor,
    pipeline_parallel: serving.parallelism.pipeline,
    context_length: serving.context_length,
    quantization: serving.quantization ?? "",
  }
}
