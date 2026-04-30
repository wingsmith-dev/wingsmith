import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useCursorList } from "@/lib/paginate"
import {
  cancelOperation,
  createOperation,
  fetchAllocation,
  fetchAllocations,
  fetchComponents,
  fetchEvents,
  fetchOperation,
  fetchOperations,
  fetchOperationsSummary,
  type AllocationFilters,
  type ComponentFilters,
  type EventFilters,
  type OperationFilters,
} from "./api"
import { isOpen, type Operation, type OperationCreateRequest } from "./types"

/** How often an unfinished operation is re-read. Polling a drain is the point. */
const POLL_MS = 4000

export function useOperations(filters: OperationFilters = {}) {
  return useCursorList(["operations", filters], (cursor) =>
    fetchOperations(filters, cursor)
  )
}

/**
 * The phase rollup and 24h terminal counters, polled on the same cadence as an
 * open operation so the tiles keep pace with a drain or roll in flight.
 */
export function useOperationsSummary(cluster?: string) {
  return useQuery({
    queryKey: ["operations", "summary", cluster ?? null],
    queryFn: () => fetchOperationsSummary(cluster),
    refetchInterval: POLL_MS,
  })
}

/**
 * One operation, polled while it is unfinished and left alone once it reaches
 * a terminal phase — the observed half stops moving there.
 */
export function useOperation(ref: string) {
  return useQuery({
    queryKey: ["operations", "detail", ref],
    queryFn: () => fetchOperation(ref),
    enabled: !!ref,
    refetchInterval: (query) => {
      const data = query.state.data as Operation | undefined
      return data && isOpen(data.phase) ? POLL_MS : false
    },
  })
}

/**
 * The operations still open against one target. Its own query rather than a
 * slice of whatever the history table happened to load: a resource page has to
 * answer "is something already running here?" before it offers to start
 * another, and it keeps polling until nothing is.
 */
export function useOpenOperations(target: string | undefined) {
  const query = useQuery({
    queryKey: ["operations", "open", target],
    queryFn: () => fetchOperations({ target: target! }, undefined, 50),
    enabled: !!target,
    refetchInterval: (q) => {
      const data = q.state.data as { items: Operation[] } | undefined
      return data?.items.some((operation) => isOpen(operation.phase))
        ? POLL_MS
        : false
    },
  })
  const operations = (query.data?.items ?? []).filter((operation) =>
    isOpen(operation.phase)
  )
  return {
    operations,
    types: operations.map((operation) => operation.type as string),
    isLoading: query.isLoading,
  }
}

export function useCreateOperation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: OperationCreateRequest) => createOperation(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["operations"] }),
  })
}

/**
 * Invalidates on settle rather than on success. Cancellation is applied before
 * the response is built, so a failed reply does not mean a failed cancellation
 * — re-reading is the only way to learn which it was, and guessing from the
 * mutation's outcome would show a phase the plane disagrees with.
 */
export function useCancelOperation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => cancelOperation(ref),
    onSettled: () => client.invalidateQueries({ queryKey: ["operations"] }),
  })
}

export function useEvents(filters: EventFilters = {}) {
  return useCursorList(["events", filters], (cursor) =>
    fetchEvents(filters, cursor)
  )
}

/**
 * Everything that has happened to one deployment, newest first.
 *
 * Both halves of the timeline come back from this one call, because both are
 * written to the same log: what a person asked for (`deployment.created`,
 * `deployment.scaled`, `deployment.dispatched`) and what a cluster concluded
 * (`deployment.pending`, `deployment.placed`, `deployment.failed`). A reader
 * should not have to know that those facts have different writers.
 *
 * Polled while the deployment is not yet serving. That is when the timeline is
 * being watched and when it is still moving; once replicas are ready the
 * interesting events have already happened.
 */
export function useDeploymentEvents(
  deployment: string | undefined,
  settled: boolean
) {
  return useCursorList(
    ["events", "deployment", deployment],
    (cursor) => fetchEvents({ subject: deployment! }, cursor),
    {
      enabled: !!deployment,
      pollWhile: () => !settled,
      pollMs: 5000,
    }
  )
}

/**
 * Everything that has happened to one workload, its deployments, its replicas
 * and the weights they load — newest first.
 *
 * A workload id used to return nothing at all: no writer files an event whose
 * subject is a workload, so the page that most needed a timeline was the one
 * page guaranteed to show an empty one. The plane now expands a `wkl_` subject
 * over the whole tree, so this is the same call it always was and only the
 * answer changed.
 *
 * Polled while the workload is not yet serving, for the same reason the
 * deployment timeline is: that is when it is moving and when somebody is
 * watching it.
 */
export function useWorkloadEvents(
  workload: string | undefined,
  settled: boolean
) {
  return useCursorList(
    ["events", "workload", workload],
    (cursor) => fetchEvents({ subject: workload! }, cursor),
    {
      enabled: !!workload,
      pollWhile: () => !settled,
      pollMs: 5000,
    }
  )
}

export function useAllocations(filters: AllocationFilters = {}) {
  return useCursorList(["allocations", filters], (cursor) =>
    fetchAllocations(filters, cursor)
  )
}

export function useAllocation(ref: string) {
  return useQuery({
    queryKey: ["allocations", "detail", ref],
    queryFn: () => fetchAllocation(ref),
    enabled: !!ref,
  })
}

export function useComponents(filters: ComponentFilters = {}) {
  return useCursorList(["components", filters], (cursor) =>
    fetchComponents(filters, cursor)
  )
}
