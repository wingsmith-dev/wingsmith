import { api, type ListResponse } from "@/lib/api"
import type {
  Policy,
  PriorityClass,
  Project,
  ProjectUpdateRequest,
  Quota,
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

export function updateProject(
  ref: string,
  body: ProjectUpdateRequest,
  revision?: string
): Promise<Project> {
  return api.patch<Project>(`/projects/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
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

export function updateQuota(
  ref: string,
  body: QuotaUpdateRequest,
  revision?: string
): Promise<Quota> {
  return api.patch<Quota>(`/quotas/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function fetchPriorityClasses(
  filters: PriorityClassFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<PriorityClass>> {
  return api.list<PriorityClass>("/priority-classes", { ...filters, cursor, limit })
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
