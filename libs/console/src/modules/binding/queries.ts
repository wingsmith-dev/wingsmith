import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import { fetchEndpoints } from "@/modules/demand/api"
import {
  createPolicy,
  createPriorityClass,
  createProject,
  createTenant,
  createQuota,
  createServingQuota,
  deletePolicy,
  deletePriorityClass,
  deleteProject,
  deleteTenant,
  deleteQuota,
  deleteServingQuota,
  fetchPolicies,
  fetchPriorityClasses,
  fetchProjects,
  fetchTenant,
  fetchTenantKeys,
  fetchTenants,
  mintTenantKey,
  revokeTenantKey,
  fetchQuotas,
  fetchResolvedServingQuotas,
  fetchServingQuotas,
  updatePolicy,
  updatePriorityClass,
  updateProject,
  updateTenant,
  updateQuota,
  updateServingQuota,
  type PolicyFilters,
  type PriorityClassFilters,
  type ProjectFilters,
  type QuotaFilters,
  type ResolvedServingQuotaParams,
  type ServingQuotaFilters,
  type TenantFilters,
} from "./api"
import type {
  PolicyCreateRequest,
  PolicyUpdateRequest,
  PriorityClassCreateRequest,
  PriorityClassUpdateRequest,
  ProjectCreateRequest,
  ProjectUpdateRequest,
  QuotaCreateRequest,
  QuotaUpdateRequest,
  ServingQuotaCreateRequest,
  ServingQuotaUpdateRequest,
  TenantAPIKeyCreateRequest,
  TenantCreateRequest,
  TenantUpdateRequest,
} from "./types"

export function useTenants(filters: TenantFilters = {}) {
  return useCursorList(["tenants", filters], (cursor) =>
    fetchTenants(filters, cursor)
  )
}

/** Every tenant, drained: the project form picks one from this. */
export function useTenantOptions() {
  return useAllPages(["tenants", "options"], (cursor) =>
    fetchTenants({}, cursor, 200)
  )
}

export function useTenant(ref: string) {
  return useQuery({
    queryKey: ["tenants", "detail", ref],
    queryFn: () => fetchTenant(ref),
    enabled: !!ref,
  })
}

export function useCreateTenant() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: TenantCreateRequest) => createTenant(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["tenants"] }),
  })
}

/**
 * Carries the tenant's `revision` as `If-Match`, so an edit written against a
 * stale read is rejected by the plane, not silently overwriting it.
 */
export function useUpdateTenant() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: TenantUpdateRequest
      revision?: string
    }) => updateTenant(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["tenants"] }),
  })
}

/** Invalidates projects too: the plane refuses while any still hang off it. */
export function useDeleteTenant() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteTenant(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["tenants"] })
      client.invalidateQueries({ queryKey: ["projects"] })
    },
  })
}

export function useTenantKeys(ref: string) {
  return useQuery({
    queryKey: ["tenants", "keys", ref],
    queryFn: () => fetchTenantKeys(ref),
    enabled: !!ref,
  })
}

/** The mint response carries the plaintext once; nothing later ever will. */
export function useMintTenantKey(ref: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: TenantAPIKeyCreateRequest) => mintTenantKey(ref, body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["tenants", "keys", ref] }),
  })
}

export function useRevokeTenantKey(ref: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (keyRef: string) => revokeTenantKey(ref, keyRef),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["tenants", "keys", ref] }),
  })
}

export function useProjects(filters: ProjectFilters = {}) {
  return useCursorList(["projects", filters], (cursor) =>
    fetchProjects(filters, cursor)
  )
}

/** Every project, drained: deployments and quotas reference them by id. */
export function useProjectOptions() {
  return useAllPages(["projects", "options"], (cursor) =>
    fetchProjects({}, cursor, 200)
  )
}

export function useCreateProject() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: ProjectCreateRequest) => createProject(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["projects"] }),
  })
}

/**
 * Carries the project's `revision` as `If-Match`, so an edit written against a
 * stale read is rejected by the plane, not silently overwriting it.
 */
export function useUpdateProject() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: ProjectUpdateRequest
      revision?: string
    }) => updateProject(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["projects"] }),
  })
}

export function useDeleteProject() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteProject(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["projects"] })
      client.invalidateQueries({ queryKey: ["quotas"] })
    },
  })
}

export function useQuotas(filters: QuotaFilters = {}) {
  return useCursorList(["quotas", filters], (cursor) =>
    fetchQuotas(filters, cursor)
  )
}

export function useCreateQuota() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: QuotaCreateRequest) => createQuota(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["quotas"] }),
  })
}

export function useUpdateQuota() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: QuotaUpdateRequest
      revision?: string
    }) => updateQuota(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["quotas"] }),
  })
}

export function useDeleteQuota() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteQuota(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["quotas"] }),
  })
}

/**
 * Traffic, not devices. Kept under its own query key so nothing invalidates one
 * kind of quota by touching the other.
 */
export function useServingQuotas(filters: ServingQuotaFilters = {}) {
  return useCursorList(["serving-quotas", filters], (cursor) =>
    fetchServingQuotas(filters, cursor)
  )
}

/** Every endpoint, drained: a serving quota is declared against exactly one. */
export function useServingEndpointOptions() {
  return useAllPages(["endpoints", "options"], (cursor) =>
    fetchEndpoints({}, cursor, 200)
  )
}

export function useCreateServingQuota() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: ServingQuotaCreateRequest) => createServingQuota(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["serving-quotas"] }),
  })
}

export function useUpdateServingQuota() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: ServingQuotaUpdateRequest
      revision?: string
    }) => updateServingQuota(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["serving-quotas"] }),
  })
}

export function useDeleteServingQuota() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteServingQuota(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["serving-quotas"] }),
  })
}

/**
 * The declared commitment resolved against a fleet the caller names. The share
 * arithmetic belongs to the plane: the console asks instead of dividing, so
 * the console and the gateway cannot disagree about what a replica enforces.
 */
export function useResolvedServingQuotas(
  params: ResolvedServingQuotaParams,
  options: { enabled?: boolean } = {}
) {
  return useQuery({
    queryKey: ["serving-quotas", "resolved", params],
    queryFn: () => fetchResolvedServingQuotas(params),
    enabled: options.enabled ?? true,
  })
}

export function usePriorityClasses(filters: PriorityClassFilters = {}) {
  return useCursorList(["priority-classes", filters], (cursor) =>
    fetchPriorityClasses(filters, cursor)
  )
}

/** Every class, drained: a project's default is chosen from these by name. */
export function usePriorityClassOptions() {
  return useAllPages(["priority-classes", "options"], (cursor) =>
    fetchPriorityClasses({}, cursor, 200)
  )
}

export function useCreatePriorityClass() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: PriorityClassCreateRequest) => createPriorityClass(body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["priority-classes"] }),
  })
}

export function useUpdatePriorityClass() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: PriorityClassUpdateRequest
      revision?: string
    }) => updatePriorityClass(vars.ref, vars.body, vars.revision),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["priority-classes"] }),
  })
}

export function useDeletePriorityClass() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deletePriorityClass(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["priority-classes"] })
      client.invalidateQueries({ queryKey: ["projects"] })
    },
  })
}

export function usePolicies(filters: PolicyFilters = {}) {
  return useCursorList(["policies", filters], (cursor) =>
    fetchPolicies(filters, cursor)
  )
}

export function useCreatePolicy() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: PolicyCreateRequest) => createPolicy(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["policies"] }),
  })
}

export function useUpdatePolicy() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: PolicyUpdateRequest
      revision?: string
    }) => updatePolicy(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["policies"] }),
  })
}

export function useDeletePolicy() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deletePolicy(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["policies"] }),
  })
}
