import { api, type ListResponse } from "@/lib/api"
import type {
  BenchmarkCreateRequest,
  BenchmarkHarness,
  BenchmarkRun,
} from "./types"

export interface BenchmarkFilters {
  deployment?: string
  cluster?: string
  harness?: string
  phase?: string
  actor?: string
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

/** What can be run and what it accepts. The form is rendered from this. */
export function fetchHarnesses(): Promise<ListResponse<BenchmarkHarness>> {
  return api.list<BenchmarkHarness>("/benchmarks/harnesses", {})
}

export function createBenchmark(
  data: BenchmarkCreateRequest
): Promise<BenchmarkRun> {
  return api.post<BenchmarkRun>("/benchmarks", data)
}
