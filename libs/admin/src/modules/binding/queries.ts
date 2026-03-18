import { useQuery } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  fetchPolicies,
  fetchPolicy,
  fetchPriorityClasses,
  fetchProject,
  fetchProjects,
  fetchQuota,
  fetchQuotas,
  type PolicyFilters,
  type PriorityClassFilters,
  type ProjectFilters,
  type QuotaFilters,
} from "./api"

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

export function usePriorityClasses(filters: PriorityClassFilters = {}) {
  return useCursorList(["priority-classes", filters], (cursor) =>
    fetchPriorityClasses(filters, cursor)
  )
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
