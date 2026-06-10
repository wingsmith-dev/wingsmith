/** `ArtifactLocationSchema`. No id: `uri` is unique within the artifact. */
export interface ArtifactLocation {
  scheme: string
  uri: string
  region: string | null
  preferred: boolean
}

/**
 * `ArtifactUpstreamSchema` — what the publisher said this release does.
 *
 * The reference, never our configuration: a workload's `context_length` is only a
 * finding when there is something to compare it against. It is `metadata`'s
 * opposite in the way `imported_args` is `engine_args` — one is a claim somebody
 * signed, the other is a bag — which is why the provenance triple
 * (`source_uri`, `recorded_by`, `recorded_at`) is part of the block and not
 * optional decoration. Nothing here reaches a cluster.
 *
 * `licence` is deliberately a second spelling of `Artifact.licence`: that one is
 * what we believe applies, this one is what the model card said, and a
 * difference between them is a finding rather than a bug. `parameter_count` is
 * not duplicated — it is already a field on the artifact.
 */
export interface ArtifactUpstream {
  provider: string | null
  provider_model_id: string | null
  context_length: number | null
  max_output_tokens: number | null
  input_modalities: string[]
  /** Null is "the publisher did not say", which is not the same as `false`. */
  reasoning: boolean | null
  tool_calling: boolean | null
  structured_output_modes: string[]
  licence: string | null
  released_at: string | null
  source_uri: string | null
  recorded_by: string | null
  recorded_at: string | null
}

/** The write shape of `upstream`. Replaced wholesale, the way locations are. */
export type ArtifactUpstreamRequest = Partial<ArtifactUpstream>

/**
 * `ArtifactBaselineSchema` — one declared quality floor, per dataset and metric.
 *
 * Declared, not observed and not derived: somebody asserts that these weights
 * score this number on this eval and signs it, so an acceptance review is an
 * argument about the assertion rather than about whichever number a harness
 * produced last.
 *
 * `tolerance_pct` is the recorder's own per-row slack and is **not** the
 * acceptance floor — that is a property of the criteria version, resolved when a
 * verdict is derived, and it is `ACCEPTANCE_FLOOR` x this value.
 */
export interface ArtifactBaseline {
  id: string
  artifact_id: string
  dataset: string
  metric: string
  value: number
  tolerance_pct: number
  source_uri: string | null
  recorded_by: string
  recorded_at: string
  note: string | null
  created_at: string
  updated_at: string | null
  revision: string
}

export interface ArtifactBaselineCreateRequest {
  dataset: string
  metric: string
  value: number
  tolerance_pct?: number
  source_uri?: string | null
  recorded_by: string
  recorded_at?: string | null
  note?: string | null
}

/**
 * Dataset and metric are absent: they are the row's identity, unique per
 * artifact, so moving one is a different baseline and not an edit to this one.
 */
export interface ArtifactBaselineUpdateRequest {
  value?: number
  tolerance_pct?: number
  source_uri?: string | null
  recorded_by?: string
  recorded_at?: string | null
  note?: string | null
}

/** `ArtifactSchema` — immutable, content-addressed bytes. Identity is the digest. */
export interface Artifact {
  id: string
  digest: string
  verified: boolean
  name: string
  kind: string
  format: string | null
  size_bytes: number
  licence: string | null
  parameter_count: number | null
  base_artifact_id: string | null
  project_id: string | null
  /** Absent on sparse payloads: the plane may return an artifact with no upstream. */
  upstream?: ArtifactUpstream
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string | null
  revision: string
  locations: ArtifactLocation[]
}

/**
 * `ArtifactUsageSchema` — what depends on this artifact, counted at read time.
 * `workload_count` is the distinct workloads serving these weights,
 * `downstream_count` the artifacts derived from this one.
 */
export interface ArtifactUsage {
  artifact_id: string
  workload_count: number
  downstream_count: number
}

/**
 * The two questions a fitment matrix can answer, and they are not one question.
 *
 * `fleet` is *what can I run right now* — every class a cluster has reported, where
 * a no-fit row is a dead end. `catalog` is *what hardware would I need* — every
 * class the contract table names as well, where a fit on a class we own none of is
 * a procurement answer rather than a deployable one.
 */
export const FITMENT_SCOPES = ["fleet", "catalog"] as const

export type FitmentScope = (typeof FITMENT_SCOPES)[number]

/**
 * `ArtifactFitLayoutSchema` — one of the several ways a class can hold this artifact.
 *
 * A fit's own fields are the recommendation: the fewest cards carrying the full
 * window. These are the rest of them, ascending, and the difference between them is a
 * real decision — four H200 at 32k leaves half the node for somebody else, eight at
 * 131k does not. Every entry serves; nothing that cannot is listed.
 *
 * `weights_gb` and `kv_counted` are not repeated here. They belong to the artifact
 * rather than to the layout and are the same for every entry.
 */
export interface ArtifactFitLayout {
  device_count: number
  node_count: number
  tensor_parallel: number
  pipeline_parallel: number
  aggregate_gb: number
  required_gb: number
  context_length: number
  /** This count buys its cards back with a shorter window than the one asked for. */
  reduced: boolean
  /** The fleet holds fewer devices of the class than this layout needs. */
  supply_short: boolean
  reason: string
}

/**
 * `ArtifactFitSchema` — whether this artifact serves on one class of card.
 *
 * The fit half is durable and the three counts are the fleet half. They stay apart
 * because `fits` with `devices_allocatable: 0` is a capacity problem and `fits:
 * false` is a physics problem, and only one of them is fixed by waiting.
 *
 * `devices_allocatable` is not a promise that a replica of `device_count` devices
 * can be placed — contiguity inside a topology domain decides that, and placement
 * preview is what answers it.
 */
export interface ArtifactFit {
  device_class: string
  label: string
  fits: boolean
  /**
   * The half of `fits` that is arithmetic rather than estimate: the weights sit on the
   * cards, and the attention heads divide across the tensor group. False is a refusal
   * the plane will make too. `fits` false with `possible` true is the sizing's opinion
   * about the window, which is advice and is overrulable.
   */
  possible: boolean
  /** The window had to be cut below the one asked for. Deliberately not a failure. */
  reduced: boolean
  device_count: number
  /**
   * Nodes the replica spans. The unit that gets ordered and racked, and the number to
   * lead with — "4 nodes of B300" is an order, "32 devices" is a number to divide.
   */
  node_count: number
  /**
   * How big one node was taken to be. Read from the fleet's observed topology for a class
   * we run, assumed otherwise. Eight on an HGX board, seventy-two on a GB300 NVL72 — the
   * interconnect is what makes a node a node, and the rack has one.
   */
  devices_per_node: number
  /** Inside a node, where the interconnect is coherent. Capped by `devices_per_node`. */
  tensor_parallel: number
  /** Across nodes, over the backend fabric. Those nodes are one connected unit. */
  pipeline_parallel: number
  memory_mib_per_device: number
  aggregate_gb: number
  weights_gb: number
  required_gb: number
  context_length: number
  /**
   * False when the artifact published no attention shape, so `required_gb` is the
   * weights and nothing else and `context_length` is zero. Such a row is a floor —
   * the real requirement is strictly larger — and showing it as a full sizing would
   * promise a window nobody costed.
   */
  kv_counted: boolean
  reason: string
  devices_total: number
  devices_allocatable: number
  cluster_count: number
  /**
   * The fleet holds this class and fewer of it than one replica needs — a fit
   * nobody can run today. Not the same as owning none of the class, which is a
   * purchase, and not the same as the free count being low, which waiting fixes.
   */
  supply_short: boolean
  /** False for hardware the contract table has never named, fitted from what a cluster reported. */
  in_catalog: boolean
  /**
   * Every count on this class that serves, the recommendation included. The fields
   * above are one of these; this is the choice between context and cards.
   */
  layouts: ArtifactFitLayout[]
}

/**
 * `ArtifactFitmentSchema` — the classes an artifact could be served on, ranked.
 *
 * Every input is echoed because a fit is meaningless without them: the same
 * artifact fits very different hardware at 8k and at 131k, and a matrix that did
 * not say which would be read as a property of the weights.
 *
 * `sizable` is false only when the artifact carries no parameter count at all —
 * `items` is then empty and `note` says why. A missing attention shape is not that
 * case: the weights still bound which cards are too small, so the matrix comes back
 * with every row's `kv_counted` false and `note` saying the windows are uncosted.
 */
export interface ArtifactFitment {
  artifact_id: string
  scope: FitmentScope
  sizable: boolean
  quantization: string
  context_length: number
  concurrency: number
  max_devices: number
  /** How many classes were ranked. `items` is the head of that list, not all of it. */
  considered: number
  note: string | null
  items: ArtifactFit[]
}

/**
 * One row of a fitment, carried into a workload's serving block as a starting point.
 *
 * The fit told us these four things and nothing else — which card, how many, at what
 * window, at what precision. Everything else about serving is still the person's to
 * declare, which is why this is six fields rather than a whole draft.
 */
export interface ServingFootprint {
  device_class: string
  device_count: number
  tensor_parallel: number
  pipeline_parallel: number
  context_length: number
  quantization: string
}

/** Query parameters for a fitment. Every one is optional and defaulted by the plane. */
export interface FitmentParams {
  scope?: FitmentScope
  context_length?: number
  concurrency?: number
  quantization?: string
  /** Only applies to classes the fleet has never reported; observed figures win. */
  devices_per_node?: number
  max_devices?: number
  /** Ranked rows to return. Zero or below means every one of them. */
  limit?: number
}

/** `ArtifactLocationRequest` — the write shape of a location. */
export interface ArtifactLocationRequest {
  scheme: string
  uri: string
  region?: string | null
  preferred?: boolean
}

export interface ArtifactCreateRequest {
  /** Omitted when `source` is set: the plane derives identity, the client never asserts it. */
  digest?: string
  /** `owner/name`, `hf:owner/name` or `hf://owner/name`. Mutually exclusive with `digest`. */
  source?: string
  name: string
  verified?: boolean
  kind?: string
  format?: string | null
  size_bytes?: number
  licence?: string | null
  parameter_count?: number | null
  base_artifact?: string | null
  project?: string | null
  upstream?: ArtifactUpstreamRequest
  metadata?: Record<string, unknown>
  locations?: ArtifactLocationRequest[]
}

/** The digest is identity, so it is absent here: bytes are never re-pointed. */
export interface ArtifactUpdateRequest {
  name?: string
  kind?: string
  format?: string | null
  size_bytes?: number
  licence?: string | null
  parameter_count?: number | null
  project?: string | null
  upstream?: ArtifactUpstreamRequest
  metadata?: Record<string, unknown>
  locations?: ArtifactLocationRequest[]
}

/**
 * `ServingDefaults` — serving parameters implied by the weights. A suggestion the
 * plane never applies: a workload's serving block is declared state someone owns,
 * so these seed that form and go nowhere else.
 *
 * `context_length`, `quantization` and `dtype` are read off the repository;
 * `device_count` and `device_memory_mib_min` are what serving that needs. The
 * derivation only runs that way — an artifact never trims its own context to fit
 * a card, because it knows nothing about which cards exist.
 */
export interface ServingDefaults {
  device_count: number
  device_memory_mib_min: number
  context_length: number | null
  quantization: string | null
  dtype: string | null
}

/**
 * `UpstreamDeploymentSchema` — what the corpus's own rules render for one node shape.
 *
 * A large model has no single tested parallelism, it has one per node it can run
 * on, so this is a list rather than a number. Nothing here is a fact about our
 * fleet: it says the layout works, not that we own such a node.
 */
export interface UpstreamDeployment {
  hardware: string
  display_name: string
  gpu_count: number
  tensor_parallel: number
  nodes_declared: number
  nodes_by_memory: number
  strategies: string[]
}

/**
 * `UpstreamServingSchema` — serving parameters somebody upstream has run,
 * attributed to them.
 *
 * Kept apart from `ServingDefaults` on purpose. That is the plane's own arithmetic
 * over `config.json`; this is a third party's measurement of a different question —
 * not "what do these weights need" but "what did this configuration do when we
 * served it". Merging them would produce a form where no field can say where its
 * number came from, and the numbers do not always agree.
 *
 * Every field is a claim, and the two URIs are how it is read back, which is why
 * the console shows the attribution beside any value it seeds from here.
 * `verified_hardware` names the node shapes the publisher ran on — evidence the
 * configuration works, and no evidence at all that we own such a node.
 */
export interface UpstreamServing {
  source: string
  /** The recipe on the corpus's own branch — what upstream says about this now. */
  source_uri: string
  /** The same file at the commit these values were captured from. */
  snapshot_uri: string
  model_id: string
  variant: string
  precision: string | null
  engine: string
  /**
   * The lowest vLLM release that serves these weights, as the recipe declares it.
   *
   * vLLM's own version (`0.28.0`), and not a workload's `engine_runtime_version`, which
   * names a tag of the Dynamo runtime image (`vllm-runtime:1.4.1`). The two count
   * in different sequences and one is not the other spelled differently, so this
   * is shown and never seeded into the form: `vllm-runtime:0.28.0` is an address
   * that has never existed.
   */
  engine_version_min: string | null
  nightly_required: boolean
  /**
   * The image the publisher serves this model from, when it names one of its own.
   *
   * Reported, never seeded. It is a third party's runtime with no `ai-dynamo` in
   * it, so pinning `engine_runtime_image` to this trades one startup failure for another.
   * What it supports is the negative claim — that the image the plane derives from
   * the engine and version cannot load these weights, and somebody has to supply
   * one that can.
   */
  docker_image: string | null
  tensor_parallel: number | null
  default_strategy: string | null
  deployments: UpstreamDeployment[]
  replica_memory_mib_min: number | null
  context_length: number | null
  max_model_len: number | null
  kv_cache_dtype: string | null
  tool_call_parser: string | null
  reasoning_parser: string | null
  /** The whole tested command line, including flags nothing here has a field for. */
  args: string[]
  env: Record<string, unknown>
  features_on: string[]
  features_opt_in: string[]
  strategies: string[]
  requires_single_topology_domain: boolean
  verified_hardware: string[]
}

/**
 * `ResolveNote` — one remark about a resolved source, and how much it matters.
 *
 * `info` is something the plane handled and is telling you about — a duplicate
 * copy of the weights excluded from the digest. `warn` is something that will
 * bite: weights the Hub cannot address, or a gate that needs a token. Painting
 * both amber is how a person learns to skim past the second.
 */
export interface ResolveNote {
  level: "info" | "warn"
  message: string
}

/**
 * `ResolvedArtifact` — a source manifest read as an artifact. Nothing is written.
 * `digest` is derived by the plane from the source's per-file oids, which is why
 * the form shows it read-only: a digest a person typed is a claim nobody checked.
 */
export interface ResolvedArtifact {
  reference: string
  digest: string
  name: string
  kind: string
  format: string | null
  revision: string | null
  size_bytes: number
  licence: string | null
  parameter_count: number | null
  source_scheme: string
  source_uri: string
  verified: boolean
  metadata: Record<string, unknown>
  locations: ArtifactLocationRequest[]
  serving: ServingDefaults
  /** Null when no corpus entry names these weights. Never our own arithmetic. */
  upstream: UpstreamServing | null
  /**
   * What the publisher claims, as opposed to `upstream`, which is what an engine ran.
   * Two third parties answering two questions, which is why they are two fields.
   */
  publisher: ArtifactUpstream
  existing_id: string | null
  notes: ResolveNote[]
}

/**
 * `ArtifactRefresh` — the result of `POST /artifacts/:ref/refresh`.
 *
 * `changed` is the point of the response, not decoration. A refresh that records
 * an attention shape the plane only recently learned to read leaves the artifact
 * looking identical apart from one metadata key, so a button that could only say
 * "refreshed" would leave the person guessing whether anything happened. Keys are
 * dotted for metadata (`metadata.shape`), and an empty list is a success — the
 * source still says exactly what was already recorded.
 *
 * `notes` is about the source rather than the diff: a repo that has since become
 * gated says so here even when nothing changed.
 */
export interface ArtifactRefresh {
  artifact: Artifact
  source: string
  changed: string[]
  notes: ResolveNote[]
}

/** The four kinds the plane accepts. Anything else is `409 invalid`. */
export const ARTIFACT_KINDS = [
  "adapter",
  "dataset",
  "image",
  "weights",
] as const

export interface ServingShape {
  device_class: string
  device_count: number
  device_memory_mib_min: number
  requires_single_topology_domain: boolean
  topology_domain_kind: string | null
}

export interface ServingParallelism {
  tensor: number
  pipeline: number
  expert: number
}

/** KV and prefix cache configuration. Unrelated to the weight cache. */
export interface ServingCache {
  kv_cache_dtype: string | null
  kv_cache_fraction: number | null
  prefix_cache_enabled: boolean
  prefix_cache_config: Record<string, unknown>
}

export interface ServingResources {
  cpu_cores: number | null
  memory_mib: number | null
  disk_gib: number | null
}

/**
 * `ServingReasoningSchema` — how this workload thinks, as rendered flags.
 *
 * Its own object for the reason `ServingCache` is: the fields are meaningless
 * individually and all of them are conditioned on the model being able to think
 * at all. What it *can* do is `capabilities.reasoning_supported`.
 */
export interface ServingReasoning {
  default_on: boolean
  parser: string | null
  transport: string
  think_begin_token: string | null
  think_end_token: string | null
  suppress_eos_until_think_end: boolean
}

/**
 * `ServingBehaviourSchema` — what the served API does, as the engine is told to do it.
 *
 * Every field renders to a flag or a rendered value, so honouring any of it
 * means a replica restart — which is why it sits in the serving block beside
 * `cache` rather than on the endpoint. Every default is the behaviour of a block
 * that says nothing, so it is inert until somebody fills it in.
 */
export interface ServingBehaviour {
  reasoning: ServingReasoning
  stream_usage: string
  context_overflow: string
  structured_output_backend: string | null
  tool_call_parser: string | null
  max_images_per_request: number | null
}

/**
 * `ServingCapabilitiesSchema` — what this engine x artifact is *claimed* to do.
 *
 * The same split as `engine_args` against `imported_args`, one level up: nothing
 * here becomes a flag or reaches a cluster, because a cluster has no use for a
 * claim about itself. The plane holds no table of what an engine can do, so a
 * block that is internally consistent and entirely false is accepted, stored,
 * and falsified by the first conformance run.
 */
export interface ServingCapabilities {
  reasoning_supported: boolean
  streaming_supported: boolean
  structured_output_modes: string[]
  tool_calling_supported: boolean
  tool_choice_modes: string[]
  parallel_tool_calls: boolean
  input_modalities: string[]
  prompt_cache_mode: string
  prompt_cache_ttl_seconds: number | null
  asserted_by: string | null
  asserted_at: string | null
  source_uri: string | null
}

export const REASONING_TRANSPORTS = [
  "reasoning_content",
  "inline_tags",
] as const
export const STREAM_USAGE_MODES = [
  "final_chunk",
  "every_chunk",
  "on_request",
] as const
export const CONTEXT_OVERFLOW_MODES = [
  "reject",
  "truncate_input",
  "clamp_output",
] as const
export const STRUCTURED_OUTPUT_MODES = [
  "text",
  "json_object",
  "json_schema",
] as const
export const TOOL_CHOICE_MODES = ["auto", "none", "required", "named"] as const
export const INPUT_MODALITIES = ["text", "image", "audio", "video"] as const
export const PROMPT_CACHE_MODES = ["off", "automatic", "explicit"] as const

/**
 * Parsers we have run, offered so that the common case is a pick rather than a
 * spelling test. The plane takes any string — an engine build knows parsers this
 * console does not — so every one of these fields keeps a way to type one in.
 */
export const REASONING_PARSERS = [
  "deepseek_r1",
  "glm45",
  "granite",
  "mistral",
  "qwen3",
] as const
export const TOOL_CALL_PARSERS = [
  "deepseek_v3",
  "granite",
  "hermes",
  "internlm",
  "llama3_json",
  "llama4_json",
  "mistral",
  "pythonic",
  "qwen3_coder",
] as const
export const STRUCTURED_OUTPUT_BACKENDS = [
  "guidance",
  "outlines",
  "xgrammar",
] as const

/**
 * `OverlaySchema` — passthrough for what the plane deliberately does not model.
 *
 * `pod_spec` merges over the rendered pod with strategic-merge semantics;
 * `dynamo` merges over the rendered graph deployment, where maps merge and lists
 * replace.
 */
export interface ServingOverlay {
  pod_spec: Record<string, unknown>
  dynamo: Record<string, unknown>
}

/**
 * `WorkloadServingSchema` — how one workload serves its weights, as one block.
 *
 * Everything an engine is told, and nothing about how much of it runs or where.
 * There is no object behind it and no id to point at: two workloads configured
 * the same way carry the same `serving_digest`, and that is the whole of what
 * used to be recipe reuse.
 *
 * `serving` inside `serving` is not a slip and is not a name this console chose.
 * The outer block is the whole configuration; the inner one is the typed
 * API-behaviour group that lands in the `serving` column and is hashed into the
 * digest under that name. Renaming it here for readability would leave the HTTP
 * body, the column and the digest field list each calling one thing something
 * different — and because TypeScript erases, the rename would typecheck green and
 * read `undefined` in a browser. `ServingBehaviour` is the type name; `serving`
 * is the property, because that is what arrives.
 */
export interface WorkloadServing {
  artifact_id: string
  engine: string
  engine_runtime_version: string
  engine_runtime_image: string | null
  /**
   * A second image, not a spelling of `engine_runtime_image`: a frontend and a worker are
   * separately versioned binaries. Null means the cluster decides.
   */
  frontend_image: string | null
  engine_args: Record<string, unknown>
  /** Read off a running process. Recorded in full, never rendered, never hashed. */
  imported_args: Record<string, unknown>
  quantization: string | null
  dtype: string | null
  parallelism: ServingParallelism
  context_length: number
  max_batch_tokens: number | null
  cache: ServingCache
  serving: ServingBehaviour
  /** Asserted, never rendered, and it never reaches a cluster. */
  capabilities: ServingCapabilities
  /**
   * `engine_args` with the flags the typed `serving` block dictates laid over the
   * top, keyed the way the operator keys them — without the leading dashes.
   * Derived on every read and stored nowhere, so that "which value actually took
   * effect" is something to read rather than something to reconstruct.
   */
  resolved_engine_args: Record<string, string>
  shape: ServingShape
  resources: ServingResources
  overlay: ServingOverlay
}

/**
 * The write shape of `serving`, grouped exactly like the read shape.
 *
 * Deliberately the same object in both directions: what a caller sends and what
 * it gets back are one shape, so a form can round-trip a block without a
 * translation layer that is one rename away from being wrong.
 *
 * `artifact` is here rather than on the workload because the weights and the
 * configuration that serves them are one declaration: repointing at other bytes
 * is a different serving block and a different digest.
 */
export interface WorkloadServingRequest {
  artifact: string
  engine: string
  engine_runtime_version: string
  context_length: number
  shape: ServingShape
  engine_runtime_image?: string | null
  frontend_image?: string | null
  engine_args?: Record<string, unknown>
  imported_args?: Record<string, unknown>
  quantization?: string | null
  dtype?: string | null
  parallelism?: ServingParallelism
  max_batch_tokens?: number | null
  cache?: ServingCache
  resources?: ServingResources
  overlay?: ServingOverlay
  serving?: ServingBehaviour
  capabilities?: ServingCapabilities
}

/**
 * A partial update. The artifact is absent: other bytes are a different workload.
 *
 * The sub-blocks replace whole, which is why this is `Partial` over the groups
 * rather than over their fields — `parallelism`, `cache`, `shape`, `resources`,
 * `serving` and `capabilities` are each meaningless in halves, so sending one
 * means stating it.
 */
export type WorkloadServingUpdate = Partial<
  Omit<WorkloadServingRequest, "artifact">
>

/** `Workload.kind` — one scheduling supertype across serving and batch. */
export type WorkloadKind = "service" | "task" | "gang" | (string & {})

/**
 * `Workload.phase`, derived from the deployments underneath it and stored
 * nowhere: `running` when every deployment runs, `failed` if any failed,
 * otherwise the least advanced phase among them.
 */
export type WorkloadPhase =
  "pending" | "admitted" | "placed" | "running" | "failed" | (string & {})

/**
 * How placement spends the total across the clusters a workload may use.
 * `pack` fills one cluster before reaching for the next; `spread` levels them.
 */
export type WorkloadStrategy = "pack" | "spread" | (string & {})

/** `DeploymentReplicaRollupSchema` — observed replicas, counted not listed. */
export interface DeploymentReplicaRollup {
  total: number
  ready: number
  running: number
  pending: number
  failed: number
  at_current_generation: number
}

/**
 * `WorkloadScaleSchema` — declared totals, and what the shares add up to.
 *
 * `unit` is named by kind — replicas, ranks, completions — because the three
 * numbers count different things and a bare integer is unreadable without it.
 * `placed` and `ready` are sums over the deployments and are the only place
 * summing a share against anything is legal.
 */
export interface WorkloadScale {
  unit: string
  min: number
  max: number
  desired: number
  placed: number
  ready: number
}

/**
 * `WorkloadDeploymentSchema` — one cluster's share, as seen from the workload.
 *
 * Enough to render the per-cluster table and link into the deployment; the full
 * per-cluster object is `GET /deployments/{ref}`.
 */
export interface WorkloadDeployment {
  id: string
  name: string
  cluster_id: string
  cluster_name: string
  region: string | null
  min_replicas: number
  max_replicas: number
  desired_replicas: number
  admission_phase: string
  admission_reason: string | null
  generation: number
  enabled: boolean
  authority: DeploymentAuthority
  queued_at: string | null
  admitted_at: string | null
  placed_at: string | null
  replicas: DeploymentReplicaRollup
  status: DeploymentStatus | null
}

/**
 * `WorkloadSchema` — what was asked for, and where it ended up.
 *
 * The workload is global: it names no cluster. `deployments` is the per-cluster
 * expansion placement chose, one row per cluster. A `gang` always has exactly
 * one — splitting a gang across clusters is rejected at placement, not ranked
 * down — and so does a workload with `pinned_cluster_id` set.
 */
export interface Workload {
  id: string
  name: string
  kind: WorkloadKind
  phase: WorkloadPhase
  project_id: string
  priority_class: string
  preemptible: boolean
  paused: boolean
  gang_size: number
  /**
   * `svg_<64 hex>` over everything in `serving` that reaches an engine, recomputed
   * by the plane on every write and never accepted from a client. Two workloads
   * that would produce the same running process carry the same digest; nothing
   * else identifies a configuration.
   */
  serving_digest: string
  serving: WorkloadServing
  adapter_artifact_ids: string[]
  /**
   * The model id the engine registers under. Read from the artifact's slug, never declared here.
   *
   * Reported and not accepted. A workload used to carry a nullable override of this and the plane
   * resolved between the two, which is how the id a client sends and the id a frontend registered
   * came apart. What a *client* sends is still changeable — that is the endpoint's `slug`.
   */
  served_model_name: string
  overrides: Record<string, unknown>
  labels: Record<string, string>
  strategy: WorkloadStrategy
  /** `{cluster_uid: minimum share}`. An unknown uid is ignored, never an error. */
  cluster_floors: Record<string, number>
  max_clusters: number | null
  /** The deliberate scheduler escape: checked for existence, never for capacity. */
  pinned_cluster_id: string | null
  scale: WorkloadScale
  started_at: string | null
  finished_at: string | null
  deadline_at: string | null
  created_at: string
  updated_at: string | null
  revision: string
  deployments: WorkloadDeployment[]
  /** The artifact's tag, resolved by the plane so a list needn't fetch each one. */
  artifact_tag: string | null
}

/**
 * `WorkloadPlacementNodeSchema` — one node a workload's live devices sit on.
 *
 * `device_count` is how many of the node's devices this workload holds right now,
 * derived from the live allocation rows rather than declared anywhere.
 */
export interface WorkloadPlacementNode {
  node: string
  cluster: string
  device_count: number
}

/**
 * `WorkloadReplicaPlacementSchema` — where one live allocation landed.
 *
 * `replica` is null for an allocation the cluster has not yet tied to a replica.
 * `gang_complete` says whether the allocation currently holds every device it
 * was granted, so a partially satisfied gang is visible here.
 */
export interface WorkloadReplicaPlacement {
  allocation: string
  replica: string | null
  cluster: string
  nodes: string[]
  devices: string[]
  device_count: number
  gang_complete: boolean
}

/**
 * `WorkloadPlacementSchema` — where a workload physically is, derived from its
 * live allocations. Read-only, computed at read time; only membership and
 * placement facts, no serving telemetry.
 */
export interface WorkloadPlacement {
  workload: string
  node_count: number
  gpu_count: number
  nodes: WorkloadPlacementNode[]
  replicas: WorkloadReplicaPlacement[]
}

/**
 * What it takes to run something, without saying where.
 *
 * `cluster` is optional and absent is the normal case: placement picks from the
 * serving block's shape, the project's quota, the policies on the workload and what
 * the clusters report free. Naming one is an override that forces a single deployment.
 *
 * The three scale numbers are TOTALS across every cluster this workload may use.
 */
export interface WorkloadCreateRequest {
  serving: WorkloadServingRequest
  name: string
  project: string
  cluster?: string | null
  /**
   * Narrows placement without taking the decision off it, unlike `cluster`. The plane
   * turns either list into a Policy named after the workload and attached to it, so the
   * constraint is enforced on every later expansion and readable at
   * `GET /policies?subject_type=workload`. Empty means no opinion.
   */
  allowed_regions?: string[]
  allowed_clusters?: string[]
  priority_class?: string | null
  kind?: WorkloadKind
  min_scale?: number
  max_scale?: number
  desired_scale?: number
  strategy?: WorkloadStrategy
  cluster_floors?: Record<string, number>
  max_clusters?: number | null
  enabled?: boolean
  preemptible?: boolean
  gang_size?: number
  deadline_at?: string | null
  adapter_artifacts?: string[]
  overrides?: Record<string, unknown>
  labels?: Record<string, string>
}

/**
 * A partial update. Cluster is absent by design — a deployment is workload x
 * cluster, so changing it is a new one. Scale is absent too: it is long-running
 * and can sit pending on quota forever, so it is the `workload.scale` operation
 * and not a field write.
 *
 * `serving` is present, and a write to it recomputes `serving_digest` and rolls
 * the replicas. The artifact is not: other bytes are a different workload.
 */
export interface WorkloadUpdateRequest {
  name?: string
  adapter_artifacts?: string[]
  serving?: WorkloadServingUpdate
  overrides?: Record<string, unknown>
  labels?: Record<string, string>
  priority_class?: string
  preemptible?: boolean
  deadline_at?: string | null
  strategy?: WorkloadStrategy
  cluster_floors?: Record<string, number>
  max_clusters?: number | null
}

/**
 * One workload's share on one cluster: declared spec, admission, observed replicas.
 *
 * `min_replicas` / `max_replicas` / `desired_replicas` are THIS CLUSTER'S SHARE of
 * the workload's total, never the total. Summing a share against a total, or
 * dividing one out of the other, is always a bug. Placement sets all three equal,
 * so there is no range here to render.
 *
 * Serving, adapters, overrides and labels are not here: they are workload facts,
 * read through `workload_id`. Admission is here, because it is per-cluster — a
 * workload can be running in one region and queued on quota in another.
 */
export interface Deployment {
  id: string
  name: string
  workload_id: string
  workload_name: string
  cluster_id: string
  min_replicas: number
  max_replicas: number
  desired_replicas: number
  admission_phase: string
  admission_reason: string | null
  queued_at: string | null
  admitted_at: string | null
  placed_at: string | null
  generation: number
  enabled: boolean
  created_at: string
  updated_at: string | null
  revision: string
  /** The *discovered* workload this was promoted from. Unrelated to `workload_id`. */
  adopted_workload_id: string | null
  adopted_fingerprint: string | null
  /** Who owns this: `observed` records and verifies, `managed` renders and converges. */
  authority: DeploymentAuthority
  replicas: DeploymentReplicaRollup
  /** What the cluster reports it is doing. Null until a cluster has reported at all. */
  status: DeploymentStatus | null
}

/**
 * The observed half: where the deployment is, and what is holding it there.
 *
 * `reason` and `message` are deliberately not errors. Most of a deployment's life
 * before it serves is spent waiting on an ordering — the artifact has to arrive, the
 * weights have to be resident, a node has to advertise enough accelerators — and
 * every one of those is expected. `last_error_*` is for the things that went wrong.
 *
 * `reason` is a closed set the operator owns and is typed as a string on purpose: a
 * cluster runs its own build and may report a sub-state this console has never heard
 * of, and showing it beats hiding it.
 */
export interface DeploymentStatus {
  observed_generation: number
  phase: string
  reason: string | null
  message: string | null
  desired_replicas: number
  ready_replicas: number
  updated_replicas: number
  unavailable_replicas: number
  last_error_code: string | null
  last_error_message: string | null
  last_error_at: string | null
  observed_at: string | null
}

/**
 * The two answers to who owns a deployment, and there is deliberately no third.
 *
 * A deployment with some fields enforced and others drifting cannot be reasoned
 * about at the moment somebody needs to (ADR 0009).
 */
export type DeploymentAuthority = "observed" | "managed"

/** Taking management, or giving it back. `fingerprint` is required only when taking it. */
export interface DeploymentAuthorityRequest {
  authority: DeploymentAuthority
  fingerprint?: string | null
}

/**
 * A partial update of one cluster's copy.
 *
 * Everything a workload declares is absent: it is declared once, globally, and a
 * per-cluster override would be a second answer to a question that already has one.
 * Scale is absent because shares are derived by placement from the workload total,
 * not asserted per cluster.
 */
export interface DeploymentUpdateRequest {
  enabled?: boolean
}

/** `EndpointTargetSchema`. Named by deployment id; the cluster is not repeated. */
export interface EndpointTarget {
  deployment_id: string
  weight: number
  role: string
  enabled: boolean
  /**
   * What the engine behind this target answers to, when the plane's answer is
   * wrong. Null means the deployment's own answer stands.
   *
   * The routing table is the plane's and the engine's name is invisible to
   * clients, so the only property that matters is that the two agree. This is
   * where a disagreement is corrected, and it costs a gateway poll — correcting
   * it on the workload instead respecs the pod and reloads the weights.
   */
  served_model_name: string | null
}

/**
 * `EndpointSurfaceSchema` — the terms on which the served API is exposed.
 *
 * Global and read by the gateway; nothing here reaches a cluster. A field
 * belongs here rather than on the workload when honouring it needs no engine flag.
 *
 * Half of it the gateway acts on and half of it is a disclosure it only reports,
 * which is why `SURFACE_ENFORCEMENT` exists and why nothing in this console
 * draws the two the same way. A disclosure rendered as a term is a lie an
 * operator repeats to a customer.
 */
export interface EndpointSurface {
  protocols: string[]
  auth_mode: string
  error_shape: string
  max_request_bytes: number | null
  request_timeout_seconds: number | null
  streaming_transport: string
  cors_enabled: boolean
  /**
   * The request fields that control generation length, most significant first.
   * Membership is support and position is precedence, which is why it is an
   * ordered list and not three switches. It names the field the serving quota's
   * `max_output_tokens` applies to; it states no number of its own.
   */
  output_length_params: string[]
}

export const ENDPOINT_PROTOCOLS = [
  "openai.chat",
  "openai.completions",
  "openai.responses",
  "openai.embeddings",
] as const
export const AUTH_MODES = ["none", "api_key", "mtls"] as const
/** One member on purpose: the gateway never reads an upstream response body. */
export const ERROR_SHAPES = ["passthrough"] as const
export const STREAMING_TRANSPORTS = ["sse", "none"] as const
export const OUTPUT_LENGTH_PARAMS = [
  "max_tokens",
  "max_completion_tokens",
  "max_output_tokens",
] as const

/**
 * `EndpointSchema` — the stable public name clients call. Global and
 * plane-only: there is no cluster on this object at any depth, because an
 * endpoint spans clusters by construction.
 */
export interface Endpoint {
  id: string
  name: string
  /**
   * The model id clients send. Never empty.
   *
   * `name` is the internal handle — URLs, metrics labels, every link in here — and is not
   * patchable. This is the public contract, and it is, because the resolved table reports it as
   * the endpoint's name and that is the key the gateway routes on. Changing it costs one gateway
   * poll and touches no cluster.
   *
   * Defaulted from the artifact behind the first target, so an endpoint over one model answers to
   * that model without anybody typing its name twice. Setting it is publishing an alias.
   */
  slug: string
  project_id: string
  visibility: string
  routing_strategy: string
  enabled: boolean
  /** Absent on sparse payloads: an endpoint may arrive with no surface declared. */
  surface?: EndpointSurface
  targets: EndpointTarget[]
  failover_order: string[]
  labels: Record<string, string>
  created_at: string
  updated_at: string | null
  revision: string
}

/** `EndpointTargetRequest` — names the deployment, never the cluster. */
export interface EndpointTargetRequest {
  deployment: string
  weight?: number
  role?: string
  enabled?: boolean
  served_model_name?: string | null
}

export interface EndpointCreateRequest {
  name: string
  project: string
  /** Empty takes the slug of the weights behind the first target. Give one only for an alias. */
  slug?: string
  visibility?: string
  routing_strategy?: string
  enabled?: boolean
  surface?: EndpointSurface
  targets?: EndpointTargetRequest[]
  failover_order?: string[]
  labels?: Record<string, string>
}

export interface EndpointUpdateRequest {
  slug?: string
  visibility?: string
  routing_strategy?: string
  enabled?: boolean
  surface?: EndpointSurface
  targets?: EndpointTargetRequest[]
  failover_order?: string[]
  labels?: Record<string, string>
}
