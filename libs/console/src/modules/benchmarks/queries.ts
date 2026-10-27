import { POLL } from "@/lib/poll"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useMemo } from "react"
import { useCursorList } from "@/lib/paginate"
import {
  createBenchmark,
  createBenchmarkRequest,
  fetchBenchmark,
  fetchBenchmarkRequest,
  fetchBenchmarkCurve,
  fetchBenchmarkRequestBands,
  fetchBenchmarkRequestVerdict,
  fetchBenchmarkVerdict,
  fetchBenchmarks,
  fetchHarnesses,
  fetchServingBenchmarkSummary,
  type BenchmarkFilters,
} from "./api"
import {
  curveOpen,
  isOpen,
  latestSteppedRequest,
  type BenchmarkCreateRequest,
  type BenchmarkCurve,
  type BenchmarkRequest,
  type BenchmarkRequestCreateRequest,
  type BenchmarkRun,
} from "./types"

/** How often an unfinished run is re-read. A benchmark takes minutes. */
const POLL_MS = POLL.progress

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
 * What a serving configuration has been measured at, by digest.
 *
 * Keyed on the digest and not on the workload, which is what makes it stable: two
 * workloads configured identically share one rollup, and it survives the workload
 * being renamed or deleted. An unknown digest is a zero count, so this is asked
 * unconditionally rather than gated on whether any run exists.
 */
export function useServingBenchmarkSummary(digest: string | undefined) {
  return useQuery({
    queryKey: ["benchmarks", "serving", digest],
    queryFn: () => fetchServingBenchmarkSummary(digest!),
    enabled: !!digest,
  })
}

/**
 * Every benchmark run taken against a workload, newest first.
 *
 * The plane filters runs by deployment, not by workload, and a workload run is
 * one run per placed deployment — so the workload's history is the union of its
 * deployments' runs, gathered here in one query and re-sorted by recency. A
 * workload with no deployments has no runs and is not fetched.
 */
export function useWorkloadBenchmarks(deployments: string[]) {
  const key = [...deployments].sort()
  return useQuery({
    queryKey: ["benchmarks", "workload", key],
    enabled: deployments.length > 0,
    queryFn: async () => {
      const pages = await Promise.all(
        deployments.map((deployment) =>
          fetchBenchmarks({ deployment }, undefined, 100)
        )
      )
      return pages
        .flatMap((page) => page.items)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
    },
    refetchInterval: (query) => {
      const runs = query.state.data as BenchmarkRun[] | undefined
      return runs?.some((run) => isOpen(run.phase)) ? POLL_MS : false
    },
  })
}

/**
 * The ask, polled while the runs it fanned out into are still moving.
 *
 * Its `phase` is derived by the plane from those runs, so it is exactly as
 * live as they are and there is nothing else on the request to wait for.
 */
export function useBenchmarkRequest(ref: string) {
  return useQuery({
    queryKey: ["benchmarks", "requests", "detail", ref],
    queryFn: () => fetchBenchmarkRequest(ref),
    enabled: !!ref,
    refetchInterval: (query) => {
      const data = query.state.data as BenchmarkRequest | undefined
      return data && isOpen(data.phase) ? POLL_MS : false
    },
  })
}

/**
 * The runs one request expanded into, ordered by the plane.
 *
 * `?request=` rather than a sub-collection, for the same reason the
 * per-deployment tab is a filter: the run is the object, and the request is one
 * of several things it is filtered by.
 */
export function useRequestRuns(request: string | undefined) {
  return useCursorList(
    ["benchmarks", "requests", "runs", request],
    (cursor) => fetchBenchmarks({ request: request! }, cursor),
    {
      enabled: !!request,
      pollWhile: (runs) => runs.some((run) => isOpen(run.phase)),
      pollMs: POLL_MS,
    }
  )
}

/**
 * The per-band re-aggregation of one request.
 *
 * Not cached long and re-read while the request is open: a band fills in as its
 * runs land, and a sweep whose last two steps have not reported is a claim
 * covering four bands out of six.
 */
export function useRequestBands(request: string | undefined, open?: boolean) {
  return useQuery({
    queryKey: ["benchmarks", "requests", "bands", request],
    queryFn: () => fetchBenchmarkRequestBands(request!),
    enabled: !!request,
    refetchInterval: open ? POLL_MS : false,
  })
}

/**
 * One request read as a load curve, polled while any of its steps is open.
 *
 * The same discipline as every other unfinished read here, keyed off the curve
 * itself rather than off a flag the caller has to keep true: a point carries
 * its own phase, so the curve says when it has stopped moving and the polling
 * ends on the read that finished it. The version is part of the key for the
 * verdict's reason — the same ramp under two standards is two answers.
 */
export function useBenchmarkCurve(
  request: string | undefined | null,
  criteria?: string
) {
  return useQuery({
    queryKey: ["benchmarks", "requests", "curve", request, criteria],
    queryFn: () => fetchBenchmarkCurve(request!, criteria),
    enabled: !!request,
    refetchInterval: (query) =>
      curveOpen(query.state.data as BenchmarkCurve | undefined)
        ? POLL_MS
        : false,
  })
}

/**
 * A request's verdict under one criteria version.
 *
 * The version is part of the key because the same request graded under two
 * standards is two answers, and caching them as one would let a "what if"
 * comparison overwrite the verdict the request was actually made under.
 */
export function useRequestVerdict(
  request: string | undefined,
  criteria?: string
) {
  return useQuery({
    queryKey: ["benchmarks", "requests", "verdict", request, criteria],
    queryFn: () => fetchBenchmarkRequestVerdict(request!, criteria),
    enabled: !!request,
  })
}

export function useBenchmarkVerdict(
  run: string | undefined,
  criteria?: string
) {
  return useQuery({
    queryKey: ["benchmarks", "verdict", run, criteria],
    queryFn: () => fetchBenchmarkVerdict(run!, criteria),
    enabled: !!run,
  })
}

/**
 * The harness catalog. Long-lived: it changes when the plane is deployed, not
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

/** The leveled ask. It expands into runs on the plane, never here. */
export function useCreateBenchmarkRequest() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (data: BenchmarkRequestCreateRequest) =>
      createBenchmarkRequest(data),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["benchmarks"] })
      client.invalidateQueries({ queryKey: ["operations"] })
    },
  })
}

/**
 * The curve of a workload's most recent ramp, if it has ever run one.
 *
 * The request is recovered from the runs rather than listed: the plane has no
 * way to enumerate requests, and a request whose runs cover more than one step
 * is a stepped one by definition. One read, shared by the tiles and the panel
 * through the query cache.
 */
export function useWorkbenchCurve(runs: BenchmarkRun[] | undefined) {
  const request = useMemo(() => latestSteppedRequest(runs), [runs])
  const query = useBenchmarkCurve(request)
  return { request, ...query }
}

export type WorkbenchCurve = ReturnType<typeof useWorkbenchCurve>
