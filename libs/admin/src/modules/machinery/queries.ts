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
  return useCursorList(["events", filters], (cursor) => fetchEvents(filters, cursor))
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
