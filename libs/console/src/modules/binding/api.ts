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
  ResolvedServingQuotaTable,
  ServingQuota,
  ServingQuotaCreateRequest,
  ServingQuotaUpdateRequest,
  Tenant,
  TenantAPIKey,
  TenantAPIKeyCreateRequest,
  TenantCreateRequest,
  TenantUpdateRequest,
} from "./types"

export interface TenantFilters {
  enabled?: boolean
  search?: string
}

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

export interface ServingQuotaFilters {
  endpoint?: string
  project?: string
  enforced?: boolean
  /** `limit_scope`, sent as `scope`. */
  scope?: string
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

export function fetchTenants(
  filters: TenantFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Tenant>> {
  return api.list<Tenant>("/tenants", { ...filters, cursor, limit })
}

export function fetchTenant(ref: string): Promise<Tenant> {
  return api.get<Tenant>(`/tenants/${encodeURIComponent(ref)}`)
}

export function createTenant(body: TenantCreateRequest): Promise<Tenant> {
  return api.post<Tenant>("/tenants", body)
}

export function updateTenant(
  ref: string,
  body: TenantUpdateRequest,
  revision?: string
): Promise<Tenant> {
  return api.patch<Tenant>(`/tenants/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deleteTenant(ref: string): Promise<void> {
  return api.delete<void>(`/tenants/${encodeURIComponent(ref)}`)
}

export function fetchTenantKeys(ref: string): Promise<TenantAPIKey[]> {
  return api.get<TenantAPIKey[]>(`/tenants/${encodeURIComponent(ref)}/keys`)
}

/** Returns the plaintext once, in `key`. It cannot be read again. */
export function mintTenantKey(
  ref: string,
  body: TenantAPIKeyCreateRequest
): Promise<TenantAPIKey> {
  return api.post<TenantAPIKey>(
    `/tenants/${encodeURIComponent(ref)}/keys`,
    body
  )
}

export function revokeTenantKey(ref: string, keyRef: string): Promise<void> {
  return api.delete<void>(
    `/tenants/${encodeURIComponent(ref)}/keys/${encodeURIComponent(keyRef)}`
  )
}

export function fetchProjects(
  filters: ProjectFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Project>> {
  return api.list<Project>("/projects", { ...filters, cursor, limit })
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

export function fetchServingQuotas(
  filters: ServingQuotaFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<ServingQuota>> {
  return api.list<ServingQuota>("/serving-quotas", {
    ...filters,
    cursor,
    limit,
  })
}

export function createServingQuota(
  body: ServingQuotaCreateRequest
): Promise<ServingQuota> {
  return api.post<ServingQuota>("/serving-quotas", body)
}

export function updateServingQuota(
  ref: string,
  body: ServingQuotaUpdateRequest,
  revision?: string
): Promise<ServingQuota> {
  return api.patch<ServingQuota>(
    `/serving-quotas/${encodeURIComponent(ref)}`,
    body,
    { ifMatch: revision }
  )
}

export function deleteServingQuota(ref: string): Promise<void> {
  return api.delete<void>(`/serving-quotas/${encodeURIComponent(ref)}`)
}

export interface ResolvedServingQuotaParams {
  endpoint?: string
  workload?: string
  replicas?: number
  replica_index?: number
}

/**
 * What each gateway replica enforces for a fleet of a stated size. The plane
 * cannot discover the replica count without putting a Kubernetes client on the
 * inference path, so the caller states it and the share arithmetic happens
 * there rather than here.
 */
export function fetchResolvedServingQuotas(
  params: ResolvedServingQuotaParams = {}
): Promise<ResolvedServingQuotaTable> {
  return api.get<ResolvedServingQuotaTable>("/serving-quotas/resolved", {
    ...params,
  })
}

export function fetchPriorityClasses(
  filters: PriorityClassFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<PriorityClass>> {
  return api.list<PriorityClass>("/priority-classes", {
    ...filters,
    cursor,
    limit,
  })
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
  return api.patch<PriorityClass>(
    `/priority-classes/${encodeURIComponent(ref)}`,
    body,
    {
      ifMatch: revision,
    }
  )
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
