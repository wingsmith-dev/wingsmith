import { api, type ListResponse } from "@/lib/api"
import type {
  Policy,
  PolicyCreateRequest,
  PolicyUpdateRequest,
  PriorityClass,
  PriorityClassCreateRequest,
  PriorityClassUpdateRequest,
  Project,
  ProjectCreateRequest,
  ProjectUpdateRequest,
  Quota,
  QuotaCreateRequest,
  QuotaUpdateRequest,
} from "./types"

export interface ProjectFilters {
  tenant?: string
  enabled?: boolean
  search?: string
}

export interface QuotaFilters {
  project?: string
  cluster?: string
  /** Sent as `class`. */
  class?: string
  over_quota_enabled?: boolean
}

export interface PriorityClassFilters {
  preemptible?: boolean
  preempts?: boolean
  search?: string
}

export interface PolicyFilters {
  enabled?: boolean
  region?: string
  class?: string
  search?: string
  subject_type?: string
  subject?: string
}

export function fetchProjects(
  filters: ProjectFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Project>> {
  return api.list<Project>("/projects", { ...filters, cursor, limit })
}

export function fetchProject(ref: string): Promise<Project> {
  return api.get<Project>(`/projects/${encodeURIComponent(ref)}`)
}

export function createProject(body: ProjectCreateRequest): Promise<Project> {
  return api.post<Project>("/projects", body)
}

export function updateProject(
  ref: string,
  body: ProjectUpdateRequest,
  revision?: string
): Promise<Project> {
  return api.patch<Project>(`/projects/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deleteProject(ref: string): Promise<void> {
  return api.delete<void>(`/projects/${encodeURIComponent(ref)}`)
}

export function fetchQuotas(
  filters: QuotaFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Quota>> {
  return api.list<Quota>("/quotas", { ...filters, cursor, limit })
}

export function fetchQuota(ref: string): Promise<Quota> {
  return api.get<Quota>(`/quotas/${encodeURIComponent(ref)}`)
}

export function createQuota(body: QuotaCreateRequest): Promise<Quota> {
  return api.post<Quota>("/quotas", body)
}

export function updateQuota(
  ref: string,
  body: QuotaUpdateRequest,
  revision?: string
): Promise<Quota> {
  return api.patch<Quota>(`/quotas/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deleteQuota(ref: string): Promise<void> {
  return api.delete<void>(`/quotas/${encodeURIComponent(ref)}`)
}

export function fetchPriorityClasses(
  filters: PriorityClassFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<PriorityClass>> {
  return api.list<PriorityClass>("/priority-classes", { ...filters, cursor, limit })
}

export function fetchPriorityClass(ref: string): Promise<PriorityClass> {
  return api.get<PriorityClass>(`/priority-classes/${encodeURIComponent(ref)}`)
}

export function createPriorityClass(
  body: PriorityClassCreateRequest
): Promise<PriorityClass> {
  return api.post<PriorityClass>("/priority-classes", body)
}

/** Conditional on `revision`; a mismatch is `409 conflict`. */
export function updatePriorityClass(
  ref: string,
  body: PriorityClassUpdateRequest,
  revision?: string
): Promise<PriorityClass> {
  return api.patch<PriorityClass>(`/priority-classes/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deletePriorityClass(ref: string): Promise<void> {
  return api.delete<void>(`/priority-classes/${encodeURIComponent(ref)}`)
}

export function fetchPolicies(
  filters: PolicyFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Policy>> {
  return api.list<Policy>("/policies", { ...filters, cursor, limit })
}

export function fetchPolicy(ref: string): Promise<Policy> {
  return api.get<Policy>(`/policies/${encodeURIComponent(ref)}`)
}

export function createPolicy(body: PolicyCreateRequest): Promise<Policy> {
  return api.post<Policy>("/policies", body)
}

export function updatePolicy(
  ref: string,
  body: PolicyUpdateRequest,
  revision?: string
): Promise<Policy> {
  return api.patch<Policy>(`/policies/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deletePolicy(ref: string): Promise<void> {
  return api.delete<void>(`/policies/${encodeURIComponent(ref)}`)
}
