import { useQuery } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  fetchArtifact,
  fetchArtifacts,
  fetchDeployment,
  fetchDeployments,
  fetchEndpoint,
  fetchEndpoints,
  fetchRecipe,
  fetchRecipes,
  type ArtifactFilters,
  type DeploymentFilters,
  type EndpointFilters,
  type RecipeFilters,
} from "./api"

export function useArtifacts(filters: ArtifactFilters = {}) {
  return useCursorList(["artifacts", filters], (cursor) =>
    fetchArtifacts(filters, cursor)
  )
}

export function useArtifact(ref: string) {
  return useQuery({
    queryKey: ["artifacts", "detail", ref],
    queryFn: () => fetchArtifact(ref),
    enabled: !!ref,
  })
}

export function useRecipes(filters: RecipeFilters = {}) {
  return useCursorList(["recipes", filters], (cursor) => fetchRecipes(filters, cursor))
}

export function useRecipe(ref: string) {
  return useQuery({
    queryKey: ["recipes", "detail", ref],
    queryFn: () => fetchRecipe(ref),
    enabled: !!ref,
  })
}

export function useRecipeOptions() {
  return useAllPages(["recipes", "options"], (cursor) => fetchRecipes({}, cursor, 200))
}

export function useDeployments(filters: DeploymentFilters = {}) {
  return useCursorList(["deployments", filters], (cursor) =>
    fetchDeployments(filters, cursor)
  )
}

export function useDeployment(ref: string, cluster?: string) {
  return useQuery({
    queryKey: ["deployments", "detail", ref, cluster],
    queryFn: () => fetchDeployment(ref, cluster),
    enabled: !!ref,
  })
}

export function useEndpoints(filters: EndpointFilters = {}) {
  return useCursorList(["endpoints", filters], (cursor) =>
    fetchEndpoints(filters, cursor)
  )
}

export function useEndpoint(ref: string) {
  return useQuery({
    queryKey: ["endpoints", "detail", ref],
    queryFn: () => fetchEndpoint(ref),
    enabled: !!ref,
  })
}
