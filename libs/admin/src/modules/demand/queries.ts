import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  createArtifact,
  createDeployment,
  createEndpoint,
  createRecipe,
  deleteArtifact,
  deleteDeployment,
  deleteEndpoint,
  deleteRecipe,
  fetchArtifact,
  fetchArtifacts,
  fetchDeployment,
  fetchDeployments,
  fetchEndpoint,
  fetchEndpoints,
  fetchRecipe,
  fetchRecipes,
  updateArtifact,
  updateDeployment,
  updateEndpoint,
  updateRecipe,
  type ArtifactFilters,
  type DeploymentFilters,
  type EndpointFilters,
  type RecipeFilters,
} from "./api"
import type {
  ArtifactCreateRequest,
  ArtifactUpdateRequest,
  DeploymentCreateRequest,
  DeploymentUpdateRequest,
  EndpointCreateRequest,
  EndpointUpdateRequest,
  RecipeCreateRequest,
  RecipeUpdateRequest,
} from "./types"

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

/** Every artifact, drained — recipes and adapter lists reference them by id. */
export function useArtifactOptions() {
  return useAllPages(["artifacts", "options"], (cursor) =>
    fetchArtifacts({}, cursor, 200)
  )
}

export function useCreateArtifact() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: ArtifactCreateRequest) => createArtifact(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["artifacts"] }),
  })
}

/**
 * Carries the artifact's `revision` as `If-Match`, so an edit written against a
 * stale read is rejected by the plane rather than silently overwriting it.
 */
export function useUpdateArtifact() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: ArtifactUpdateRequest
      revision?: string
    }) => updateArtifact(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["artifacts"] }),
  })
}

export function useDeleteArtifact() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteArtifact(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["artifacts"] })
      client.invalidateQueries({ queryKey: ["recipes"] })
    },
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

export function useCreateRecipe() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: RecipeCreateRequest) => createRecipe(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["recipes"] }),
  })
}

export function useUpdateRecipe() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: { ref: string; body: RecipeUpdateRequest; revision?: string }) =>
      updateRecipe(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["recipes"] }),
  })
}

export function useDeleteRecipe() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteRecipe(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["recipes"] })
      client.invalidateQueries({ queryKey: ["deployments"] })
    },
  })
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

/** Every deployment, drained — endpoint targets reference them by id. */
export function useDeploymentOptions() {
  return useAllPages(["deployments", "options"], (cursor) =>
    fetchDeployments({}, cursor, 200)
  )
}

export function useCreateDeployment() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: DeploymentCreateRequest) => createDeployment(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["deployments"] }),
  })
}

export function useUpdateDeployment() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: DeploymentUpdateRequest
      revision?: string
    }) => updateDeployment(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["deployments"] }),
  })
}

export function useDeleteDeployment() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteDeployment(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["deployments"] })
      client.invalidateQueries({ queryKey: ["endpoints"] })
    },
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

export function useCreateEndpoint() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: EndpointCreateRequest) => createEndpoint(body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["endpoints"] }),
  })
}

export function useUpdateEndpoint() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: EndpointUpdateRequest
      revision?: string
    }) => updateEndpoint(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["endpoints"] }),
  })
}

export function useDeleteEndpoint() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteEndpoint(ref),
    onSuccess: () => client.invalidateQueries({ queryKey: ["endpoints"] }),
  })
}
