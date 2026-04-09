/**
 * What was running when a run was taken. Snapshotted by the plane at creation
 * rather than joined, because a recipe is mutable and a deployment is
 * disposable — a run resolved by join would start describing something else.
 */
export interface BenchmarkProvenance {
  model: string
  deployment_generation: number
  recipe_id: string | null
  recipe_version: number | null
  engine_args: Record<string, unknown>
}

/**
 * One knob a harness accepts. Served by the plane so this console renders its
 * form from the same table the plane validates against, rather than carrying a
 * second copy of what AIPerf takes.
 */
export interface BenchmarkParameter {
  name: string
  type: "int" | "str" | "bool" | string
  default: string | number | boolean
  description: string
}

export interface BenchmarkHarness {
  name: string
  image: string
  description: string
  parameters: BenchmarkParameter[]
}

/**
 * A measurement of one deployment.
 *
 * `summary` is `unknown` on purpose. It is the harness's own export, stored and
 * served verbatim, and typing it here would be this console asserting it knows
 * a shape the plane deliberately refuses to normalise. Anything that reads it
 * has to cope with not recognising it.
 */
export interface BenchmarkRun {
  id: string
  deployment_id: string
  cluster_id: string
  operation_id: string | null
  harness: string
  harness_version: string | null
  image: string
  parameters: Record<string, unknown>
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

export interface BenchmarkCreateRequest {
  deployment: string
  harness: string
  image?: string | null
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
  return a.harness === b.harness && a.harness_version === b.harness_version && a.image === b.image
}
