import { api, type ListResponse } from "@/lib/api"
import type {
  BenchmarkBand,
  BenchmarkCreateRequest,
  BenchmarkHarness,
  BenchmarkRequest,
  BenchmarkRequestCreateRequest,
  BenchmarkRun,
  BenchmarkVerdict,
} from "./types"

export interface BenchmarkFilters {
  deployment?: string
  cluster?: string
  harness?: string
  phase?: string
  actor?: string
  /** The request a run was expanded from — how a parent lists its children. */
  request?: string
  since?: string
}

export function fetchBenchmarks(
  filters: BenchmarkFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<BenchmarkRun>> {
  return api.list<BenchmarkRun>("/benchmarks", { ...filters, cursor, limit })
}

export function fetchBenchmark(ref: string): Promise<BenchmarkRun> {
  return api.get<BenchmarkRun>(`/benchmarks/${encodeURIComponent(ref)}`)
}

/**
 * Whether one run met the standard, derived now and stored nowhere.
 *
 * `criteria` grades against a version other than the one its request recorded,
 * which answers "would this have passed under the new rules" — the question a
 * stored verdict cannot answer at all.
 */
export function fetchBenchmarkVerdict(
  ref: string,
  criteria?: string
): Promise<BenchmarkVerdict> {
  return api.get<BenchmarkVerdict>(
    `/benchmarks/${encodeURIComponent(ref)}/verdict`,
    criteria ? { criteria } : undefined
  )
}

/** What can be run and what it accepts. The form is rendered from this. */
export function fetchHarnesses(): Promise<ListResponse<BenchmarkHarness>> {
  return api.list<BenchmarkHarness>("/benchmarks/harnesses", {})
}

export function fetchBenchmarkRequest(ref: string): Promise<BenchmarkRequest> {
  return api.get<BenchmarkRequest>(
    `/benchmarks/requests/${encodeURIComponent(ref)}`
  )
}

export function fetchBenchmarkRequestVerdict(
  ref: string,
  criteria?: string
): Promise<BenchmarkVerdict> {
  return api.get<BenchmarkVerdict>(
    `/benchmarks/requests/${encodeURIComponent(ref)}/verdict`,
    criteria ? { criteria } : undefined
  )
}

/**
 * One request's runs re-aggregated by the input-length band each landed in.
 *
 * A separate read from the runs themselves because the re-aggregation is the
 * plane's: percentiles do not average, and a console that grouped the rows
 * itself would be a second, quieter answer to what a band measured.
 */
export function fetchBenchmarkRequestBands(
  ref: string
): Promise<ListResponse<BenchmarkBand>> {
  return api.list<BenchmarkBand>(
    `/benchmarks/requests/${encodeURIComponent(ref)}/bands`,
    {}
  )
}

export function createBenchmark(
  data: BenchmarkCreateRequest
): Promise<BenchmarkRun> {
  return api.post<BenchmarkRun>("/benchmarks", data)
}

export function createBenchmarkRequest(
  data: BenchmarkRequestCreateRequest
): Promise<BenchmarkRequest> {
  return api.post<BenchmarkRequest>("/benchmarks/requests", data)
}
