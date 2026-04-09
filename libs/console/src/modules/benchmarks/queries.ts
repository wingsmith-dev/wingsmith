import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useCursorList } from "@/lib/paginate"
import {
  createBenchmark,
  fetchBenchmark,
  fetchBenchmarks,
  fetchHarnesses,
  type BenchmarkFilters,
} from "./api"
import { isOpen, type BenchmarkCreateRequest, type BenchmarkRun } from "./types"

/** How often an unfinished run is re-read. A benchmark takes minutes. */
const POLL_MS = 5000

export function useBenchmarks(filters: BenchmarkFilters = {}) {
  return useCursorList(["benchmarks", filters], (cursor) =>
    fetchBenchmarks(filters, cursor)
  )
}

/**
 * One run, polled while it is unfinished and left alone once it is not. The
 * observed half stops moving at a terminal phase, so polling past it is asking
 * the plane a question with a fixed answer.
 */
export function useBenchmark(ref: string) {
  return useQuery({
    queryKey: ["benchmarks", "detail", ref],
    queryFn: () => fetchBenchmark(ref),
    enabled: !!ref,
    refetchInterval: (query) => {
      const data = query.state.data as BenchmarkRun | undefined
      return data && isOpen(data.phase) ? POLL_MS : false
    },
  })
}

/**
 * The runs against one deployment, newest first.
 *
 * Polled while any of them is open rather than on a fixed interval: the tab is
 * usually looked at right after starting a run, and it should fill in without
 * the page being reloaded.
 */
export function useDeploymentBenchmarks(deployment: string | undefined) {
  return useCursorList(
    ["benchmarks", "deployment", deployment],
    (cursor) => fetchBenchmarks({ deployment: deployment! }, cursor),
    {
      enabled: !!deployment,
      pollWhile: (runs) => runs.some((run) => isOpen(run.phase)),
      pollMs: POLL_MS,
    }
  )
}

/**
 * The harness catalogue. Long-lived: it changes when the plane is deployed, not
 * while somebody is filling in a form.
 */
export function useHarnesses() {
  return useQuery({
    queryKey: ["benchmarks", "harnesses"],
    queryFn: fetchHarnesses,
    staleTime: 10 * 60 * 1000,
  })
}

export function useCreateBenchmark() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (data: BenchmarkCreateRequest) => createBenchmark(data),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["benchmarks"] })
      // The run is carried by an operation, and that operation is now pending.
      client.invalidateQueries({ queryKey: ["operations"] })
    },
  })
}
