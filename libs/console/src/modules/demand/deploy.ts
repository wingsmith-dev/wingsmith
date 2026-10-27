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

import { emptyCapabilities, emptyServing } from "./display"
import type {
  Artifact,
  ArtifactUpstream,
  ServingBehaviour,
  ServingCapabilities,
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
 * The Dynamo runtime tag a derived block asks for, which is not a version of the engine.
 *
 * The plane resolves `<repository>:<engine_runtime_version>` against
 * `nvcr.io/nvidia/ai-dynamo/vllm-runtime`, whose tags are Dynamo releases — `1.4.1` —
 * while vLLM's own versions are `0.x`. The two count in different sequences.
 *
 * Never seeded from the recipe's `engine_version_min`, which is vLLM's number. Doing so
 * would make upstream evidence actively harmful: a model the corpus covered would be
 * pinned to a `vllm-runtime` tag that has never existed, while a model it did not cover
 * would fall through to `latest` and serve. The vLLM
 * floor is shown beside the field as the claim it is, and no field carries it.
 *
 * `latest` is the honest stand-in for the tag: it says "whatever the plane is
 * configured to ship" — `ENGINE_RUNTIME_TAG_DEFAULT` — rather than pinning a version
 * this code invented, and it is editable here and on the workload afterwards.
 */
export const DEFAULT_ENGINE_RUNTIME_VERSION = "latest"

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
export type SeededFrom = "derived" | "weights" | "upstream" | "publisher"

/**
 * Whether the publisher has denied a capability outright.
 *
 * Three states and only one of them blocks: `false` is somebody asserting these
 * weights cannot do it, null is the publisher never having spoken, and the
 * difference matters because a `config.json` says nothing about tools or thinking.
 * Treating silence as a denial would strip a parser vLLM has tested.
 */
function denied(claim: boolean | null | undefined): boolean {
  return claim === false
}

/**
 * The typed behaviour block a corpus entry implies, minus anything the publisher denied.
 *
 * Only the parsers, and only when the corpus names one: everything else in the
 * block is a rendered flag nobody upstream has an opinion about, and a default
 * invented here would be indistinguishable from a tested one on the form.
 *
 * A parser is dropped when the artifact's claims deny the capability it parses.
 * The two sources answer different questions — the publisher says what the model
 * is, the corpus says what one engine ran — and where they disagree the claim
 * wins, because the plane refuses a parser without the capability under it and a
 * request nobody can submit is worse than a flag nobody set.
 */
export function proposedBehaviour(
  upstream?: UpstreamServing | null,
  claims?: ArtifactUpstream | null
): ServingBehaviour {
  const block = emptyServing()
  if (!upstream) return block
  return {
    ...block,
    reasoning: {
      ...block.reasoning,
      parser: denied(claims?.reasoning) ? null : upstream.reasoning_parser,
    },
    tool_call_parser: denied(claims?.tool_calling)
      ? null
      : upstream.tool_call_parser,
  }
}

/**
 * What this workload is claimed to do, from the publisher first and the recipe second.
 *
 * The behaviour block and the capability block are one statement in two halves, and
 * the plane rejects a half: a tool call parser is meaningless on a workload that does
 * not claim tool calling. So the claim travels with the parser.
 *
 * The two sources are not interchangeable. The artifact's `upstream` is what the
 * publisher said about the weights, and it is the better answer wherever it speaks —
 * modalities and structured output have no other honest source here. The vLLM recipe
 * only narrows or corroborates: a named parser is evidence of the one capability it
 * parses, which is enough to claim it where the publisher was silent and not enough
 * to claim it where the publisher said no.
 *
 * `tool_choice_modes` is not a second decision. The plane requires it to be non-empty
 * exactly when tool calling is claimed, and an engine that emits tool calls at all
 * accepts the three unnamed modes.
 *
 * The parsers are read off `behaviour` rather than off the corpus, which is what makes
 * a person's override submittable. `proposedBehaviour` has already dropped anything the
 * publisher denied, so a denial still yields no parser and no claim; but a parser typed
 * into the form by hand arrives here as a parser, and the claim the plane requires
 * under it travels with it. Passing no block reads the corpus's own proposal, which is
 * what every caller that is not the form wants.
 */
export function proposedCapabilities(
  upstream?: UpstreamServing | null,
  claims?: ArtifactUpstream | null,
  behaviour?: ServingBehaviour
): ServingCapabilities {
  const block = emptyCapabilities()
  const effective = behaviour ?? proposedBehaviour(upstream, claims)
  const tools = claims?.tool_calling === true || !!effective.tool_call_parser
  const reasoning = claims?.reasoning === true || !!effective.reasoning.parser
  const modalities = claims?.input_modalities.length
    ? claims.input_modalities
    : block.input_modalities
  return {
    ...block,
    reasoning_supported: reasoning,
    tool_calling_supported: tools,
    tool_choice_modes: tools ? ["auto", "none", "required"] : [],
    structured_output_modes: claims?.structured_output_modes.length
      ? claims.structured_output_modes
      : block.structured_output_modes,
    // Text is always an input modality and the plane says so. A claim that lost it
    // — an empty list, or a publisher who named only the second one — would be
    // refused, and the artifact page is where that gets corrected, not here.
    input_modalities: modalities.includes("text")
      ? modalities
      : ["text", ...modalities],
    source_uri: claims?.source_uri ?? null,
  }
}

/**
 * The fields of a proposed serving block a person may disagree with, before it is sent.
 *
 * Every one of these has a proposed answer and every proposed answer can be wrong —
 * the corpus measured a different build, the publisher never spoke, the engine is not
 * the one the recipe used. `undefined` means untouched and the proposal stands; a
 * string means somebody typed it, including the empty string, which is how a field is
 * cleared back to "say nothing" rather than back to the proposal.
 *
 * Only what is proposed is here. The six numbers of the footprint belong to the
 * topology step, and the fields nothing proposes — `max_batch_tokens`,
 * `kv_cache_fraction`, `resources` — are left at the plane's default and edited on the
 * workload, because a form that asks about them before anything is running is asking
 * a question with no evidence behind it.
 */
export interface ServingOverrides {
  engine?: string
  engine_runtime_version?: string
  engine_runtime_image?: string
  frontend_image?: string
  dtype?: string
  kv_cache_dtype?: string
  /** MiB. A floor nobody states is no floor — see `perDeviceFloor`. */
  device_memory_mib_min?: number
  /** Tokens. The fit's window is a starting point, not a ceiling somebody has to accept. */
  context_length?: number
  reasoning_parser?: string
  tool_call_parser?: string
}

/** A field left untouched keeps its proposal; a field typed empty says nothing. */
function chosen(
  override: string | undefined,
  proposed: string | null
): string | null {
  if (override === undefined) return proposed
  return override.trim() || null
}

/**
 * The per-device memory floor, which is a constraint somebody states and never one derived.
 *
 * It was derived twice and it was wrong both times, in the same way. The plane's suggestion
 * sizes the declared window across the count *it* proposed; the request carries the count and
 * the window the *fit* chose. Those are different numbers about different configurations, and
 * carrying one into the other produces a floor that describes nothing — GLM-5.3-Flash asked
 * for 768 GiB to serve 383 GiB of weights, and gemma-4-26b sent to a single RTX PRO 6000 asked
 * for 122 GiB per card from a card that has 96, which the plane refuses outright with "a floor
 * of 124928 MiB excludes every rtx-pro-6000-96gb-server-edition".
 *
 * Re-dividing the replica total was the first repair and it only moved the arithmetic. The
 * real defect is that a floor is not arithmetic at all: `device_memory_mib_min` filters devices
 * *within* a class the request has already named, and a class is itself a memory figure, so
 * for everything the fit picks the floor is redundant by construction. What it is genuinely
 * for is the case the console cannot see — a cluster reporting a card of that class with less
 * memory than the catalog says, a partition, a card that came up degraded. That is a
 * thing an operator knows and states, not a thing a form can infer.
 *
 * So nothing is proposed, and the field is editable. Whether the weights fit at all is still
 * checked, twice, by the two things that can honestly answer it: the fitment offers no row
 * that does not fit, and the plane's own `check_capacity` refuses a shape whose cards cannot
 * hold the weights.
 */
function perDeviceFloor(override: number | undefined): number {
  if (override === undefined) return 0
  return override > 0 ? Math.ceil(override) : 0
}

/**
 * The serving block that a fitment row plus an artifact imply, and what was overridden.
 *
 * The fit answers the six things it is — which card, how many, how they are split,
 * at what window and precision — and everything else comes from the proposal, from an
 * override, or is left at the plane's default. Left alone matters: `max_batch_tokens`,
 * `kv_cache_fraction`, `resources` and `topology_domain_kind` are never guessed,
 * because neither the console nor the corpus has an honest value and the plane's
 * own default beats a number invented here.
 *
 * The proposal is a starting point and never a floor. This is called on every render
 * of the params step with whatever has been typed so far, so the request the form
 * shows and the request it sends are the same object, computed the same way — a form
 * that displayed one block and submitted another is the defect this shape prevents.
 *
 * `prefix_cache_enabled` is the one opinion: on. It is free on a repeated prompt and
 * the engines default it on themselves.
 */
export function derivedServingRequest(
  artifact: Artifact,
  footprint: ServingFootprint,
  proposal: ServingProposal = {},
  overrides: ServingOverrides = {}
): WorkloadServingRequest {
  const { defaults, upstream } = proposal
  const proposedServing = proposedBehaviour(upstream, artifact.upstream)
  const serving: ServingBehaviour = {
    ...proposedServing,
    reasoning: {
      ...proposedServing.reasoning,
      parser: chosen(
        overrides.reasoning_parser,
        proposedServing.reasoning.parser
      ),
    },
    tool_call_parser: chosen(
      overrides.tool_call_parser,
      proposedServing.tool_call_parser
    ),
  }
  return {
    artifact: artifact.id,
    engine: overrides.engine ?? (upstream?.engine || DEFAULT_ENGINE),
    // Never `upstream.engine_version_min`: that is vLLM's version and this is a tag
    // of the Dynamo runtime image. See `DEFAULT_ENGINE_RUNTIME_VERSION`.
    engine_runtime_version:
      overrides.engine_runtime_version ?? DEFAULT_ENGINE_RUNTIME_VERSION,
    engine_runtime_image: chosen(overrides.engine_runtime_image, null),
    frontend_image: chosen(overrides.frontend_image, null),
    context_length: overrides.context_length ?? footprint.context_length,
    shape: {
      device_class: footprint.device_class,
      device_count: footprint.device_count,
      device_memory_mib_min: perDeviceFloor(overrides.device_memory_mib_min),
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
    dtype: chosen(overrides.dtype, defaults?.dtype ?? null),
    cache: {
      kv_cache_dtype: chosen(
        overrides.kv_cache_dtype,
        upstream?.kv_cache_dtype ?? null
      ),
      kv_cache_fraction: null,
      prefix_cache_enabled: true,
      prefix_cache_config: {},
    },
    serving,
    capabilities: proposedCapabilities(upstream, artifact.upstream, serving),
  }
}

/**
 * Where each seeded field came from, for the form to attribute beside it.
 *
 * Attribution is the whole point of seeding: a value nobody can trace is a value
 * nobody will move, and a form full of those is how an operator ends up running a
 * configuration they never chose and cannot defend.
 *
 * A field somebody has typed into is attributed to nobody, which is why `overrides`
 * is read here at all. Leaving "from vLLM" beside a value vLLM did not give is worse
 * than saying nothing: it is the one label on the form that is a claim about a third
 * party, and a wrong one survives into whatever argument the configuration is later
 * defended with.
 *
 * `engine_runtime_version` is deliberately absent whatever upstream says. The recipe's
 * `engine_version_min` is vLLM's number and this field is a Dynamo tag, so attributing
 * it to the corpus would be citing a source for a value the corpus never gave.
 */
export function servingProvenance(
  proposal: ServingProposal = {},
  claims?: ArtifactUpstream | null,
  overrides: ServingOverrides = {}
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
  if (upstream?.engine) at.engine = "upstream"
  if (upstream?.kv_cache_dtype) at.kv_cache_dtype = "upstream"
  if (upstream?.reasoning_parser) at.reasoning_parser = "upstream"
  if (upstream?.tool_call_parser) at.tool_call_parser = "upstream"
  if (upstream?.requires_single_topology_domain)
    at.requires_single_topology_domain = "upstream"

  // A capability is attributed to whoever actually claimed it, and the publisher
  // outranks the corpus: "the publisher says it calls tools" and "vLLM shipped a
  // parser for it" are different strengths of evidence and the form should not
  // render them as one.
  if (claims?.tool_calling === true) at.tool_calling_supported = "publisher"
  else if (upstream?.tool_call_parser && claims?.tool_calling !== false)
    at.tool_calling_supported = "upstream"
  if (claims?.reasoning === true) at.reasoning_supported = "publisher"
  else if (upstream?.reasoning_parser && claims?.reasoning !== false)
    at.reasoning_supported = "upstream"
  if ((claims?.input_modalities.length ?? 0) > 1)
    at.input_modalities = "publisher"
  if (claims?.structured_output_modes.length)
    at.structured_output_modes = "publisher"

  // A field somebody typed into is theirs, whatever proposed the value it replaced.
  for (const field of Object.keys(overrides) as (keyof ServingOverrides)[]) {
    if (overrides[field] !== undefined) delete at[field]
  }
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
      (workload.serving.quantization ?? "") === footprint.quantization
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
