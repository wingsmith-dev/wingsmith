/**
 * What a run is a measurement *of*, and the three are different claims.
 *
 * A deployment run measures one cluster's configuration: the device class, the
 * tensor-parallel degree, the engine flags. A workload run measures the serving
 * configuration, which is the same experiment however many clusters it was placed
 * on. An
 * endpoint run measures what a caller actually gets through the public name,
 * across whatever the gateway routed it to. Averaging any two of the three
 * describes nothing that exists, which is why the level travels with every
 * number this console draws.
 */
export type BenchmarkLevel = "deployment" | "workload" | "endpoint"

/** The shape of runs one request expands into. */
export type BenchmarkMode = "single" | "warmup" | "ramp" | "sweep" | "soak"

export type BenchmarkTier = "L1" | "L2"

export type VerdictOutcome = "pass" | "fail" | "inconclusive" | "not_applicable"

/**
 * What was running when a run was taken. Snapshotted by the plane at creation,
 * not joined, because a serving block is mutable and a deployment is
 * disposable: a run resolved by join would start describing something else.
 *
 * `serving_digest` is the whole of that attribution now, and it is strictly
 * stronger than the recipe id and version it replaces: a digest cannot drift.
 *
 * Every field is empty on an endpoint-level run. A request to a public name is
 * routed by weight across every enabled upstream, so an attribution to one of
 * them would be confidently wrong; the request's `level` is what records why
 * the snapshot is empty.
 */
export interface BenchmarkProvenance {
  model: string
  deployment_generation: number
  serving_digest: string | null
  engine_args: Record<string, unknown>
  /** The declared serving behavior block: which protocols answered, and how. */
  serving: Record<string, unknown>
}

/**
 * `ServingBenchmarkSummarySchema`: offline measurement rolled up per
 * `serving_digest`, computed at read time.
 *
 * Addressed by the digest, not by the workload that carries it, because
 * the digest is what a measurement is about: every run sharing it measured the
 * same running process. It keeps answering after the workload has been renamed,
 * rescaled or deleted, which the recipe rollup this replaces could not.
 *
 * An unknown digest is a zero count and not a 404, "how much measurement stands
 * behind this configuration" has "none" as an answer, so it is safe to ask about
 * anything. `peak_output_tokens_per_second` is null when no graded run carried a
 * reading, which is a named absence and not a zero. Held-in-plane offline
 * measurement, never live serving telemetry.
 */
export interface ServingBenchmarkSummary {
  serving_digest: string
  run_count: number
  graded_run_count: number
  peak_output_tokens_per_second: number | null
}

/**
 * One knob a harness accepts. Served by the plane so this console renders its
 * form from the same table the plane validates against, instead of carrying a
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
 * `summary` is `unknown`. It is the harness's own export, stored and
 * served verbatim, and typing it here would be this console asserting it knows
 * a shape the plane refuses to normalize by design. Anything that reads it
 * has to cope with not recognizing it.
 *
 * `deployment_id` and `cluster_id` are nullable because an endpoint-level run
 * has neither. `request_id` names the ask it was expanded from, and
 * `step_index` orders the runs of one sweep: one per input-length band.
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
  log_tail: string | null
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
  serving_digest: string | null
  artifact_id: string | null
}

/**
 * The ask. Declared, creatable, and never edited afterwards: a request that
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
 * `POST /v1/benchmarks/requests`: what to measure and at which level.
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
 * One number a curve read resolved, or a named absence where it could not.
 *
 * `value` is null and `path` still says where the plane looked, which is the
 * whole discipline: a reading that could not be resolved is a miss with a name,
 * never a zero. A chart draws it as a gap, because a zero plotted where nothing
 * was measured is a lie about the hardware.
 */
export interface BenchmarkReading {
  value: number | null
  unit: string
  /** The dotted path into the harness export the plane read, resolved or not. */
  path: string
}

/**
 * The readings of one curve point, keyed by the plane's reading id.
 *
 * Open to ids this console does not know: the map is served, and a version of
 * the plane that resolves one more number should not have to wait for a console
 * release. An id absent from the map is the same fact as one whose value is
 * null, nothing was read, and `reading()` is how both are asked.
 */
export interface BenchmarkCurveReadings {
  [id: string]: BenchmarkReading | undefined
}

/**
 * One step of a stepped request, as the load curve needs it.
 *
 * `step_value` is the load that was applied and not the order it was applied
 * in: a request asking for `[16, 4, 64]` keeps that order, so `step_index` is
 * position and `step_value` is the x axis.
 *
 * A step whose run is still open carries its phase and all-null readings, which
 * is what lets a running ramp draw itself in progressively, not appear
 * whole at the end.
 */
export interface BenchmarkCurvePoint {
  step_index: number
  /** Null when the run carried no resolved value for the field its request walked. */
  step_value: number | null
  run: string
  phase: string
  /** A warm-up: drawn so it can be seen, joined to no line and counted by nothing. */
  discard: boolean
  readings: BenchmarkCurveReadings
}

/**
 * Where the curve turned, derived at read against the pinned criteria version
 * and stored nowhere.
 *
 * Always an object, never null. `found` is false for two different reasons,
 * every step stayed under the threshold, or too few steps succeeded to say,
 * and `reason` is the only thing that tells them apart, so nothing in this
 * console renders the absence of a knee without it.
 */
export interface BenchmarkKnee {
  found: boolean
  step_index: number | null
  step_value: number | null
  method: string
  metric: string
  threshold: number | null
  reason: string
}

/**
 * A stepped request read as one curve, derived now.
 *
 * `step_field` names the parameter the request walked, so the x axis is
 * labeled by the plane instead of hardcoded here: the same read serves a
 * concurrency ramp and an input-length sweep, and a console that assumed
 * concurrency would mislabel half of them.
 */
export interface BenchmarkCurve {
  request: string
  serving_digest: string | null
  step_field: string
  criteria_version: string
  points: BenchmarkCurvePoint[]
  knee: BenchmarkKnee
}

/** One reading of one point, with an absent id and a null value the same answer. */
export function reading(point: BenchmarkCurvePoint, id: string): number | null {
  return point.readings[id]?.value ?? null
}

/** The unit the plane quoted a reading in, or nothing if it never resolved one. */
export function readingUnit(
  point: BenchmarkCurvePoint,
  id: string
): string | null {
  return point.readings[id]?.unit || null
}

/**
 * How much of a ramp has landed: the counted steps that reached a terminal
 * phase, out of the counted steps there are.
 *
 * Derived from the points because the points already say it. Warm-ups are
 * excluded from both halves, for the same reason no aggregate counts them: a
 * ramp of eight steps behind one warm-up is eight steps of progress, not nine.
 */
export function curveProgress(curve: BenchmarkCurve | undefined): {
  done: number
  total: number
  open: number
  failed: number
  discarded: number
} {
  const points = curve?.points ?? []
  const counted = points.filter((point) => !point.discard)
  return {
    done: counted.filter((point) => !isOpen(point.phase)).length,
    total: counted.length,
    open: counted.filter((point) => isOpen(point.phase)).length,
    failed: counted.filter(
      (point) => !isOpen(point.phase) && point.phase !== "succeeded"
    ).length,
    discarded: points.length - counted.length,
  }
}

/**
 * Whether the curve is still moving, and so still worth re-reading.
 *
 * A curve with no steps at all counts as moving. The plane expands a request
 * into its runs after accepting it, so a ramp read in the moment between the
 * two comes back empty, and a reader that stopped there would leave the panel
 * saying "not expanded yet" until somebody reloaded the page. Emptiness is the
 * one state that cannot say it is finished.
 */
export function curveOpen(curve: BenchmarkCurve | undefined): boolean {
  if (!curve) return false
  if (curve.points.length === 0) return true
  return curve.points.some((point) => isOpen(point.phase))
}

/**
 * The most recent stepped request among a workload's runs, or nothing.
 *
 * The plane has no way to list requests, so the ask is recovered from the runs
 * it expanded into: a request is stepped when its runs cover more than one
 * step, which is a fact the runs carry and needs no new endpoint. Recency is
 * the newest run in the group, because a workload ramp fans out and its runs do
 * not all start at once.
 */
export function latestSteppedRequest(
  runs: BenchmarkRun[] | undefined
): string | null {
  const groups = new Map<string, { steps: Set<number>; at: string }>()
  for (const run of runs ?? []) {
    if (!run.request_id) continue
    const group = groups.get(run.request_id) ?? {
      steps: new Set<number>(),
      at: "",
    }
    group.steps.add(run.step_index)
    const at = run.started_at ?? run.created_at
    if (at > group.at) group.at = at
    groups.set(run.request_id, group)
  }
  let best: string | null = null
  let bestAt = ""
  for (const [request, group] of groups) {
    if (group.steps.size < 2) continue
    if (group.at > bestAt) {
      best = request
      bestAt = group.at
    }
  }
  return best
}
