import { api, type ListResponse } from "@/lib/api"
import type {
  Artifact,
  ArtifactCreateRequest,
  ArtifactUpdateRequest,
  Deployment,
  DeploymentUpdateRequest,
  Endpoint,
  EndpointCreateRequest,
  EndpointUpdateRequest,
  Recipe,
  RecipeCreateRequest,
  RecipeUpdateRequest,
  ResolvedArtifact,
  Workload,
  WorkloadCreateRequest,
  WorkloadUpdateRequest,
} from "./types"

export interface ArtifactFilters {
  kind?: string
  /** Sent as `format`. */
  format?: string
  project?: string
  search?: string
  label?: string[]
}

export interface RecipeFilters {
  artifact?: string
  engine?: string
  class?: string
  search?: string
  label?: string[]
}

export interface WorkloadFilters {
  project?: string
  kind?: string
  phase?: string
  recipe?: string
  /** Workloads with a share on this cluster. Global object, per-cluster question. */
  cluster?: string
  label?: string[]
}

/** No `type`: kind is a workload fact and a deployment no longer carries it. */
export interface DeploymentFilters {
  cluster?: string
  workload?: string
  project?: string
  phase?: string
  label?: string[]
}

export interface EndpointFilters {
  project?: string
  artifact?: string
  search?: string
  label?: string[]
}

export function fetchArtifacts(
  filters: ArtifactFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Artifact>> {
  return api.list<Artifact>("/artifacts", { ...filters, cursor, limit })
}

export function fetchArtifact(ref: string): Promise<Artifact> {
  return api.get<Artifact>(`/artifacts/${encodeURIComponent(ref)}`)
}

/**
 * Reads a source slug as a proposed artifact. Writes nothing, so it is safe to
 * call while someone is still typing into the form.
 */
export function resolveArtifact(source: string): Promise<ResolvedArtifact> {
  return api.get<ResolvedArtifact>("/artifacts/resolve", { source })
}

export function createArtifact(body: ArtifactCreateRequest): Promise<Artifact> {
  return api.post<Artifact>("/artifacts", body)
}

/** Conditional on `revision`; a mismatch is `409 conflict`. */
export function updateArtifact(
  ref: string,
  body: ArtifactUpdateRequest,
  revision?: string
): Promise<Artifact> {
  return api.patch<Artifact>(`/artifacts/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deleteArtifact(ref: string): Promise<void> {
  return api.delete<void>(`/artifacts/${encodeURIComponent(ref)}`)
}

export function fetchRecipes(
  filters: RecipeFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Recipe>> {
  return api.list<Recipe>("/recipes", { ...filters, cursor, limit })
}

export function fetchRecipe(ref: string): Promise<Recipe> {
  return api.get<Recipe>(`/recipes/${encodeURIComponent(ref)}`)
}

export function createRecipe(body: RecipeCreateRequest): Promise<Recipe> {
  return api.post<Recipe>("/recipes", body)
}

export function updateRecipe(
  ref: string,
  body: RecipeUpdateRequest,
  revision?: string
): Promise<Recipe> {
  return api.patch<Recipe>(`/recipes/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deleteRecipe(ref: string): Promise<void> {
  return api.delete<void>(`/recipes/${encodeURIComponent(ref)}`)
}

export function fetchWorkloads(
  filters: WorkloadFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Workload>> {
  return api.list<Workload>("/workloads", { ...filters, cursor, limit })
}

/** `{ref}` is a `wkl_` uid or a name; a bare name needs `?project=` beside it. */
export function fetchWorkload(ref: string, project?: string): Promise<Workload> {
  return api.get<Workload>(`/workloads/${encodeURIComponent(ref)}`, { project })
}

export function createWorkload(body: WorkloadCreateRequest): Promise<Workload> {
  return api.post<Workload>("/workloads", body)
}

export function updateWorkload(
  ref: string,
  body: WorkloadUpdateRequest,
  revision?: string
): Promise<Workload> {
  return api.patch<Workload>(`/workloads/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deleteWorkload(ref: string): Promise<void> {
  return api.delete<void>(`/workloads/${encodeURIComponent(ref)}`)
}

export function fetchDeployments(
  filters: DeploymentFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Deployment>> {
  return api.list<Deployment>("/deployments", { ...filters, cursor, limit })
}

/**
 * A deployment name is unique per cluster, not globally, so a name reference
 * needs `?cluster=` alongside it. An id needs nothing.
 */
export function fetchDeployment(
  ref: string,
  cluster?: string
): Promise<Deployment> {
  return api.get<Deployment>(`/deployments/${encodeURIComponent(ref)}`, {
    cluster,
  })
}

/**
 * The only write a deployment takes on its own. There is no create and no
 * delete: placement creates a deployment when it expands a workload, and
 * removing one is a `workload.drain` or a scale change.
 */
export function updateDeployment(
  ref: string,
  body: DeploymentUpdateRequest,
  revision?: string
): Promise<Deployment> {
  return api.patch<Deployment>(
    `/deployments/${encodeURIComponent(ref)}`,
    body,
    {
      ifMatch: revision,
    }
  )
}

export function fetchEndpoints(
  filters: EndpointFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Endpoint>> {
  return api.list<Endpoint>("/endpoints", { ...filters, cursor, limit })
}

export function fetchEndpoint(ref: string): Promise<Endpoint> {
  return api.get<Endpoint>(`/endpoints/${encodeURIComponent(ref)}`)
}

export function createEndpoint(body: EndpointCreateRequest): Promise<Endpoint> {
  return api.post<Endpoint>("/endpoints", body)
}

export function updateEndpoint(
  ref: string,
  body: EndpointUpdateRequest,
  revision?: string
): Promise<Endpoint> {
  return api.patch<Endpoint>(`/endpoints/${encodeURIComponent(ref)}`, body, {
    ifMatch: revision,
  })
}

export function deleteEndpoint(ref: string): Promise<void> {
  return api.delete<void>(`/endpoints/${encodeURIComponent(ref)}`)
}
