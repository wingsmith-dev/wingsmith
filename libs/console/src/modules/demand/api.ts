import { api, type ListResponse } from "@/lib/api"
import type {
  Artifact,
  ArtifactBaseline,
  ArtifactBaselineCreateRequest,
  ArtifactBaselineUpdateRequest,
  ArtifactCreateRequest,
  ArtifactFitment,
  ArtifactRefresh,
  ArtifactUpdateRequest,
  ArtifactUsage,
  Deployment,
  DeploymentUpdateRequest,
  Endpoint,
  EndpointCreateRequest,
  EndpointUpdateRequest,
  ResolvedArtifact,
  FitmentParams,
  Workload,
  WorkloadCreateRequest,
  WorkloadPlacement,
  WorkloadRevision,
  WorkloadRevisionUpdate,
  WorkloadUpdateRequest,
} from "./types"

export interface ArtifactFilters {
  kind?: string
  /** Sent as `format`. */
  format?: string
  project?: string
  search?: string
  /** Artifacts derived from this one — quantizations and fine-tunes naming it as base. */
  base_artifact?: string
  label?: string[]
}

export interface WorkloadFilters {
  project?: string
  kind?: string
  phase?: string
  /** Workloads serving these weights — the reverse of the artifact's usage count. */
  artifact?: string
  /**
   * Every workload configured exactly this way — an exact match on the whole
   * `svg_...` string, which is what reuse is now.
   */
  serving_digest?: string
  /** The device class the workload asks for, spelled the way the class routes spell it. */
  class?: string
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
  /** The reverse index: endpoints whose targets point at this workload's deployments. */
  workload?: string
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

/** Counted at read time, stored nowhere: workloads and downstream artifacts. */
export function fetchArtifactUsage(ref: string): Promise<ArtifactUsage> {
  return api.get<ArtifactUsage>(`/artifacts/${encodeURIComponent(ref)}/usage`)
}

/**
 * The device classes this artifact could be served on, ranked, derived at read time.
 *
 * Reserves nothing and decides nothing — a class this calls a fit still has to
 * survive placement. `scope` picks which question is being asked; the rest are the
 * terms of the fit, and the plane defaults every one of them.
 */
export function fetchArtifactFitment(
  ref: string,
  params: FitmentParams = {}
): Promise<ArtifactFitment> {
  return api.get<ArtifactFitment>(
    `/artifacts/${encodeURIComponent(ref)}/fitment`,
    { ...params }
  )
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

/**
 * Re-reads the artifact's own source and records what the plane can now derive.
 *
 * No body: the source is already on the artifact, and naming a different one would
 * be a way to move an artifact onto weights it was not registered from. A source
 * that now resolves to a different digest is a `409` and nothing is written.
 */
export function refreshArtifact(ref: string): Promise<ArtifactRefresh> {
  return api.post<ArtifactRefresh>(
    `/artifacts/${encodeURIComponent(ref)}/refresh`,
    {}
  )
}

export function deleteArtifact(ref: string): Promise<void> {
  return api.delete<void>(`/artifacts/${encodeURIComponent(ref)}`)
}

/**
 * Baselines are a sub-resource rather than a field on the artifact: the set is
 * genuinely plural, one row per dataset and metric, and each row carries its own
 * tolerance and its own provenance. Editing one is not a rewrite of the rest.
 */
export function fetchArtifactBaselines(
  ref: string,
  cursor?: string,
  limit?: number
): Promise<ListResponse<ArtifactBaseline>> {
  return api.list<ArtifactBaseline>(
    `/artifacts/${encodeURIComponent(ref)}/baselines`,
    { cursor, limit }
  )
}

export function createArtifactBaseline(
  ref: string,
  body: ArtifactBaselineCreateRequest
): Promise<ArtifactBaseline> {
  return api.post<ArtifactBaseline>(
    `/artifacts/${encodeURIComponent(ref)}/baselines`,
    body
  )
}

export function updateArtifactBaseline(
  ref: string,
  baseline: string,
  body: ArtifactBaselineUpdateRequest,
  revision?: string
): Promise<ArtifactBaseline> {
  return api.patch<ArtifactBaseline>(
    `/artifacts/${encodeURIComponent(ref)}/baselines/${encodeURIComponent(baseline)}`,
    body,
    { ifMatch: revision }
  )
}

export function deleteArtifactBaseline(
  ref: string,
  baseline: string
): Promise<void> {
  return api.delete<void>(
    `/artifacts/${encodeURIComponent(ref)}/baselines/${encodeURIComponent(baseline)}`
  )
}

export function fetchWorkloads(
  filters: WorkloadFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Workload>> {
  return api.list<Workload>("/workloads", { ...filters, cursor, limit })
}

/** `{ref}` is a `wkl_` uid or a name; a bare name needs `?project=` beside it. */
export function fetchWorkload(
  ref: string,
  project?: string
): Promise<Workload> {
  return api.get<Workload>(`/workloads/${encodeURIComponent(ref)}`, { project })
}

/**
 * Where a workload physically landed, derived from its live allocations. A bare
 * name needs `?project=` beside it, the same as `fetchWorkload`.
 */
export function fetchWorkloadPlacement(
  ref: string,
  project?: string
): Promise<WorkloadPlacement> {
  return api.get<WorkloadPlacement>(
    `/workloads/${encodeURIComponent(ref)}/placement`,
    { project }
  )
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

/**
 * Every serving configuration this workload has held, newest first.
 *
 * There is no reapply endpoint beside this one and there does not need to be: each entry's
 * `serving` is a patch body, so going back is `updateWorkload` carrying it.
 */
export function fetchWorkloadRevisions(
  ref: string,
  project?: string,
  cursor?: string,
  limit?: number
): Promise<ListResponse<WorkloadRevision>> {
  return api.list<WorkloadRevision>(
    `/workloads/${encodeURIComponent(ref)}/revisions`,
    { project, cursor, limit }
  )
}

/** One revision, by `wkr_` id or by the number the history lists it under. */
export function fetchWorkloadRevision(
  ref: string,
  revision: string,
  project?: string
): Promise<WorkloadRevision> {
  return api.get<WorkloadRevision>(
    `/workloads/${encodeURIComponent(ref)}/revisions/${encodeURIComponent(revision)}`,
    { project }
  )
}

/** Writes why a configuration was chosen. The only write a revision accepts. */
export function annotateWorkloadRevision(
  ref: string,
  revision: string,
  body: WorkloadRevisionUpdate,
  project?: string
): Promise<WorkloadRevision> {
  return api.patch<WorkloadRevision>(
    `/workloads/${encodeURIComponent(ref)}/revisions/${encodeURIComponent(revision)}`,
    body,
    { query: { project } }
  )
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
