import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  createPolicy,
  createPriorityClass,
  createProject,
  createQuota,
  deletePolicy,
  deletePriorityClass,
  deleteProject,
  deleteQuota,
  fetchPolicies,
  fetchPolicy,
  fetchPriorityClass,
  fetchPriorityClasses,
  fetchProject,
  fetchProjects,
  fetchQuota,
  fetchQuotas,
  updatePolicy,
  updatePriorityClass,
  updateProject,
  updateQuota,
  type PolicyFilters,
  type PriorityClassFilters,
  type ProjectFilters,
  type QuotaFilters,
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
} from "./types"

export function useProjects(filters: ProjectFilters = {}) {
  return useCursorList(["projects", filters], (cursor) =>
    fetchProjects(filters, cursor)
  )
}

/** Every project, drained — deployments and quotas reference them by id. */
export function useProjectOptions() {
  return useAllPages(["projects", "options"], (cursor) =>
    fetchProjects({}, cursor, 200)
  )
}

export function useProject(ref: string) {
  return useQuery({
    queryKey: ["projects", "detail", ref],
    queryFn: () => fetchProject(ref),
    enabled: !!ref,
  })
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
 * stale read is rejected by the plane rather than silently overwriting it.
 */
export function useUpdateProject() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: { ref: string; body: ProjectUpdateRequest; revision?: string }) =>
      updateProject(vars.ref, vars.body, vars.revision),
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
  return useCursorList(["quotas", filters], (cursor) => fetchQuotas(filters, cursor))
}

export function useQuota(ref: string) {
  return useQuery({
    queryKey: ["quotas", "detail", ref],
    queryFn: () => fetchQuota(ref),
    enabled: !!ref,
  })
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
    mutationFn: (vars: { ref: string; body: QuotaUpdateRequest; revision?: string }) =>
      updateQuota(vars.ref, vars.body, vars.revision),
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

export function usePriorityClasses(filters: PriorityClassFilters = {}) {
  return useCursorList(["priority-classes", filters], (cursor) =>
    fetchPriorityClasses(filters, cursor)
  )
}

export function usePriorityClass(ref: string) {
  return useQuery({
    queryKey: ["priority-classes", "detail", ref],
    queryFn: () => fetchPriorityClass(ref),
    enabled: !!ref,
  })
}

/** Every class, drained — a project's default is chosen from these by name. */
export function usePriorityClassOptions() {
  return useAllPages(["priority-classes", "options"], (cursor) =>
    fetchPriorityClasses({}, cursor, 200)
  )
}

export function useCreatePriorityClass() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: PriorityClassCreateRequest) => createPriorityClass(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["priority-classes"] }),
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
    onSuccess: () => client.invalidateQueries({ queryKey: ["priority-classes"] }),
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

export function usePolicy(ref: string) {
  return useQuery({
    queryKey: ["policies", "detail", ref],
    queryFn: () => fetchPolicy(ref),
    enabled: !!ref,
  })
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
    mutationFn: (vars: { ref: string; body: PolicyUpdateRequest; revision?: string }) =>
      updatePolicy(vars.ref, vars.body, vars.revision),
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
