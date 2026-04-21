/**
 * What a run is a measurement *of*, and the three are different claims.
 *
 * A deployment run measures one cluster's configuration — the device class, the
 * tensor-parallel degree, the engine flags. A workload run measures the recipe,
 * which is the same experiment however many clusters it was placed on. An
 * endpoint run measures what a caller actually gets through the public name,
 * across whatever the gateway routed it to. Averaging any two of the three
 * describes nothing that exists, which is why the level travels with every
 * number this console draws.
 */
export type BenchmarkLevel = "deployment" | "workload" | "endpoint"

/** The shape of runs one request expands into. */
export type BenchmarkMode = "single" | "warmup" | "ramp" | "sweep" | "soak"

export type BenchmarkTier = "L1" | "L2"

export type VerdictOutcome =
  | "pass"
  | "fail"
  | "inconclusive"
  | "not_applicable"

/**
 * What was running when a run was taken. Snapshotted by the plane at creation
 * rather than joined, because a recipe is mutable and a deployment is
 * disposable — a run resolved by join would start describing something else.
 *
 * Every field is empty on an endpoint-level run. A request to a public name is
 * routed by weight across every enabled upstream, so an attribution to one of
 * them would be confidently wrong; the request's `level` is what records why
 * the snapshot is empty.
 */
export interface BenchmarkProvenance {
  model: string
  deployment_generation: number
  recipe_id: string | null
  recipe_version: number | null
  engine_args: Record<string, unknown>
  /** The recipe's declared serving block — which protocols answered, and how. */
  serving: Record<string, unknown>
}

/**
 * One knob a harness accepts. Served by the plane so this console renders its
 * form from the same table the plane validates against, rather than carrying a
 * second copy of what AIPerf takes.
 *
 * `levels` narrows a knob to a subset of its harness's own; empty means every
 * level the harness declares.
 */
export interface BenchmarkParameter {
  name: string
  type: "int" | "str" | "bool" | string
  default: string | number | boolean
  description: string
  levels: string[]
}

export interface BenchmarkHarness {
  name: string
  image: string
  description: string
  /** Where this harness may be pointed. Served, never assumed here. */
  levels: string[]
  parameters: BenchmarkParameter[]
  max_probes: number | null
}

/**
 * One measurement.
 *
 * Observed, and so read-only everywhere: there is no update and no delete on
 * the wire, and this console offers no affordance that suggests otherwise.
 *
 * `summary` is `unknown` on purpose. It is the harness's own export, stored and
 * served verbatim, and typing it here would be this console asserting it knows
 * a shape the plane deliberately refuses to normalise. Anything that reads it
 * has to cope with not recognising it.
 *
 * `deployment_id` and `cluster_id` are nullable because an endpoint-level run
 * has neither. `request_id` names the ask it was expanded from, and
 * `step_index` orders the runs of one sweep — one per input-length band.
 */
export interface BenchmarkRun {
  id: string
  request_id: string | null
  deployment_id: string | null
  cluster_id: string | null
  endpoint_id: string | null
  operation_id: string | null
  harness: string
  harness_version: string | null
  image: string
  parameters: Record<string, unknown>
  step_index: number
  vantage: string | null
  /** A warm-up: kept so it can be read, counted by no aggregate anywhere. */
  discard: boolean
  requested_by: string
  reason: string | null
  provenance: BenchmarkProvenance
  phase: string
  summary: Record<string, unknown> | null
  artifacts_uri: string | null
  error: string | null
  created_at: string
  updated_at: string | null
  started_at: string | null
  finished_at: string | null
}

/**
 * One thing that was behind the target when a request expanded, snapshotted
 * then and allowed to go stale loudly. A workload rescaled mid-run does not
 * retroactively change what was measured.
 */
export interface BenchmarkCoverage {
  deployment_id: string | null
  cluster_id: string | null
  recipe_id: string | null
  recipe_version: number | null
  artifact_id: string | null
}

/**
 * The ask. Declared, creatable, and never edited afterwards — a request that
 * changed its harness or its criteria after its runs finished would rewrite
 * what those runs claim to be.
 *
 * It carries no verdict and no summary of its own. `phase` is derived from its
 * runs by the plane, because a column here would be a third writer on a
 * lifecycle that already has two.
 */
export interface BenchmarkRequest {
  id: string
  level: BenchmarkLevel
  target_id: string
  harness: string
  image: string
  parameters: Record<string, unknown>
  mode: BenchmarkMode
  /** The frozen standard its verdict is derived against. */
  criteria_version: string
  tier: BenchmarkTier | null
  /** False when two upstreams ran different artifacts, which no score belongs to. */
  homogeneous: boolean
  coverage: BenchmarkCoverage[]
  requested_by: string
  reason: string | null
  phase: string
  runs: string[]
  created_at: string
  updated_at: string | null
}

/** One graded assertion, with the number it graded and the number it graded against. */
export interface BenchmarkCheck {
  id: string
  label: string
  outcome: VerdictOutcome
  observed: number | null
  threshold: number | null
  comparator: string
  detail: string | null
}

/**
 * Whether the standard was met, derived at read and stored nowhere.
 *
 * `criteria_version` is not decoration. A verdict shown without the version it
 * was derived under is unfalsifiable, so nothing in this console renders the
 * outcome without it.
 */
export interface BenchmarkVerdict {
  outcome: VerdictOutcome
  criteria_version: string
  reason: string | null
  checks: BenchmarkCheck[]
}

/**
 * One input-length band of a request, re-aggregated from the runs in it.
 *
 * `ttft_p90_ms` and `ttft_p50_ms` are the worst of the band because percentiles
 * do not average; `ttft_avg_ms` is weighted by request count, which does.
 * `incomplete` names the runs that reported no time-to-first-token at all.
 */
export interface BenchmarkBand {
  band: string
  runs: number
  requests: number
  ttft_p90_ms: number | null
  ttft_p50_ms: number | null
  ttft_avg_ms: number | null
  incomplete: string[]
}

export interface BenchmarkCreateRequest {
  deployment: string
  harness: string
  image?: string | null
  reason?: string | null
  parameters?: Record<string, unknown>
}

/**
 * `POST /v1/benchmarks/requests` — what to measure and at which level.
 *
 * `steps` are the load points a stepped mode walks: concurrencies for a `ramp`,
 * prompt lengths for a `sweep`. A sweep is what makes a per-band
 * time-to-first-token claim evidenceable at all.
 */
export interface BenchmarkRequestCreateRequest {
  level: BenchmarkLevel
  target: string
  harness: string
  image?: string | null
  mode?: BenchmarkMode
  steps?: number[]
  criteria?: string | null
  tier?: BenchmarkTier | null
  reason?: string | null
  parameters?: Record<string, unknown>
}

const OPEN_PHASES = new Set(["pending", "running"])

/** Whether a run is still moving, and so still worth polling. */
export function isOpen(phase: string): boolean {
  return OPEN_PHASES.has(phase)
}

/**
 * Whether two runs may be compared.
 *
 * The plane's guarantee is narrow and this is it: same harness, same version,
 * same image. A release that redefined a percentile is exactly what a same-tool
 * comparison gets wrong without this check, and across harnesses there is no
 * shared definition to compare against at all. The image is part of it because
 * a run may override the harness's pinned one, and two runs that pulled
 * different bytes are not the same tool however their versions read.
 */
export function comparable(a: BenchmarkRun, b: BenchmarkRun): boolean {
  return (
    a.harness === b.harness &&
    a.harness_version === b.harness_version &&
    a.image === b.image
  )
}

/**
 * The runs an aggregate may count.
 *
 * A discarded run is a warm-up. It is kept and shown — the warm-up is sometimes
 * itself the thing that failed — and it is counted by nothing.
 */
export function counted(runs: BenchmarkRun[]): BenchmarkRun[] {
  return runs.filter((run) => !run.discard)
}

/**
 * The one target a request names, whichever of the three it is.
 *
 * A run carries the ids it has and nulls for the ones its level does not, so
 * asking a run "what were you measuring" without its request gives a partial
 * answer at deployment level and an empty one at endpoint level.
 */
export function runTarget(run: BenchmarkRun): string | null {
  return run.endpoint_id ?? run.deployment_id ?? null
}
