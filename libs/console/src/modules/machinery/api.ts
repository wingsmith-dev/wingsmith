import { api, type ListResponse } from "@/lib/api"
import type {
  Allocation,
  Component,
  Operation,
  OperationCreateRequest,
  OperationSummary,
  PlaneEvent,
} from "./types"

export interface OperationFilters {
  cluster?: string
  /** The operation type, sent as `type`. */
  type?: string
  phase?: string
  target?: string
  actor?: string
  since?: string
}

export interface EventFilters {
  cluster?: string
  subject?: string
  kind?: string
  severity?: string
  since?: string
  until?: string
}

export interface AllocationFilters {
  cluster?: string
  project?: string
  /** Sent as `class`. */
  class?: string
  phase?: string
  over_quota?: boolean
  live?: boolean
  replica?: string
  device?: string
}

export interface ComponentFilters {
  cluster?: string
  name?: string
  kind?: string
  behind?: boolean
  enabled?: boolean
  search?: string
}

export function fetchOperations(
  filters: OperationFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Operation>> {
  return api.list<Operation>("/operations", { ...filters, cursor, limit })
}

export function fetchOperation(ref: string): Promise<Operation> {
  return api.get<Operation>(`/operations/${encodeURIComponent(ref)}`)
}

/** `GET /v1/operations/summary`: phase rollup and the 24h terminal counters. */
export function fetchOperationsSummary(
  cluster?: string
): Promise<OperationSummary> {
  return api.get<OperationSummary>("/operations/summary", { cluster })
}

/**
 * `POST /v1/operations` with a type and a target, never a verb on the
 * target's own path. The reply is an Operation with an id and a phase, and the
 * caller polls it.
 */
export function createOperation(
  body: OperationCreateRequest,
  idempotencyKey?: string
): Promise<Operation> {
  return api.post<Operation>("/operations", body, { idempotencyKey })
}

/**
 * Cancellation, and intentionally not deletion: deleting the row would throw
 * away who asked for this and why, which is the reason the object exists.
 */
export function cancelOperation(ref: string): Promise<Operation> {
  return api.delete<Operation>(`/operations/${encodeURIComponent(ref)}`)
}

export function fetchEvents(
  filters: EventFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<PlaneEvent>> {
  return api.list<PlaneEvent>("/events", { ...filters, cursor, limit })
}

export function fetchAllocations(
  filters: AllocationFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Allocation>> {
  return api.list<Allocation>("/allocations", { ...filters, cursor, limit })
}

export function fetchAllocation(ref: string): Promise<Allocation> {
  return api.get<Allocation>(`/allocations/${encodeURIComponent(ref)}`)
}

export function fetchComponents(
  filters: ComponentFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Component>> {
  return api.list<Component>("/components", { ...filters, cursor, limit })
}
