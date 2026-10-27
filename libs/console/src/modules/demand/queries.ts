import { POLL } from "@/lib/poll"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useMemo } from "react"
import { api } from "@/lib/api"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  annotateWorkloadRevision,
  createArtifact,
  createEndpoint,
  createWorkload,
  deleteArtifact,
  deleteEndpoint,
  deleteWorkload,
  fetchArtifact,
  fetchArtifactBaselines,
  fetchArtifactFitment,
  fetchArtifactUsage,
  fetchArtifacts,
  fetchDeployment,
  fetchDeployments,
  fetchEndpoint,
  fetchEndpoints,
  fetchWorkload,
  fetchWorkloadPlacement,
  fetchWorkloadRevisions,
  fetchWorkloads,
  refreshArtifact,
  resolveArtifact,
  updateArtifact,
  updateEndpoint,
  updateWorkload,
  type ArtifactFilters,
  type DeploymentFilters,
  type EndpointFilters,
  type WorkloadFilters,
} from "./api"
import { artifactSource } from "./display"
import type { Artifact, FitmentParams } from "./types"
import type {
  ArtifactCreateRequest,
  ArtifactUpdateRequest,
  ArtifactUsage,
  Deployment,
  EndpointCreateRequest,
  EndpointUpdateRequest,
  Workload,
  WorkloadCreateRequest,
  WorkloadRevisionUpdate,
  WorkloadUpdateRequest,
} from "./types"

/** Fast enough to watch a deployment come up, slow enough not to poll the plane for sport. */
const POLL_MS = POLL.progress

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

/**
 * One artifact's dependants — workloads and downstream artifacts — counted at
 * read time by the plane rather than stored on the object.
 */
export function useArtifactUsage(ref: string) {
  return useQuery({
    queryKey: ["artifacts", "usage", ref],
    queryFn: () => fetchArtifactUsage(ref),
    enabled: !!ref,
  })
}

/**
 * The device classes one artifact could be served on, ranked by the plane.
 *
 * Keyed on the terms as well as the ref: the same artifact fits very different
 * hardware at 8k and at 131k, so caching two answers under one key would show the
 * previous question's matrix while the new one loads. Nothing here writes, so it is
 * safe to re-run while somebody is still dragging a slider.
 */
export function useArtifactFitment(ref: string, params: FitmentParams = {}) {
  return useQuery({
    queryKey: ["artifacts", "fitment", ref, params],
    queryFn: () => fetchArtifactFitment(ref, params),
    enabled: !!ref,
  })
}

/** The plane answers at most this many artifacts per batched usage read. */
const USAGE_BATCH = 200

/**
 * Usage for a set of artifacts, read back as a map by id. One request per 200
 * artifacts through `GET /artifacts/usage?id=...`, not one per row, so
 * a list page costs the same however many rows it shows. An id the plane does
 * not know is simply absent from the map.
 */
export function useArtifactUsages(ids: string[]) {
  const unique = useMemo(() => [...new Set(ids.filter(Boolean))].sort(), [ids])
  const query = useQuery({
    queryKey: ["artifacts", "usage", "batch", unique],
    queryFn: async () => {
      const batches: string[][] = []
      for (let i = 0; i < unique.length; i += USAGE_BATCH)
        batches.push(unique.slice(i, i + USAGE_BATCH))
      const pages = await Promise.all(
        batches.map((id) =>
          api.get<{ items: ArtifactUsage[] }>("/artifacts/usage", { id })
        )
      )
      return pages.flatMap((page) => page.items)
    },
    enabled: unique.length > 0,
  })
  return useMemo(
    () =>
      new Map<string, ArtifactUsage>(
        (query.data ?? []).map((usage) => [usage.artifact_id, usage])
      ),
    [query.data]
  )
}

/** Artifacts derived from this one — the quantizations and fine-tunes that name it as base. */
export function useDerivedArtifacts(baseArtifact: string) {
  return useCursorList(
    ["artifacts", "derived", baseArtifact],
    (cursor) => fetchArtifacts({ base_artifact: baseArtifact }, cursor),
    { enabled: !!baseArtifact }
  )
}

/**
 * The distinct quantizations declared for each artifact, read from the workloads
 * serving it. Quantization is a serving property, not an artifact one, so the list
 * column groups one drained workload read by artifact instead of asking the plane
 * once per row. It shares its cache entry with `useWorkloadOptions`.
 */
export function useArtifactQuantizations(ids: string[]) {
  const workloads = useWorkloadOptions()
  return useMemo(() => {
    const sets = new Map(ids.map((id) => [id, new Set<string>()]))
    for (const workload of workloads.data ?? []) {
      const quantization = workload.serving.quantization
      if (quantization)
        sets.get(workload.serving.artifact_id)?.add(quantization)
    }
    return new Map([...sets].map(([id, set]) => [id, [...set]]))
  }, [ids, workloads.data])
}

/** Every artifact, drained — workloads and adapter lists reference them by id. */
export function useArtifactOptions() {
  return useAllPages(["artifacts", "options"], (cursor) =>
    fetchArtifacts({}, cursor, 200)
  )
}

/**
 * A mutation rather than a query even though it only reads: resolving walks a
 * whole repo tree, so it happens when someone asks for it and its `isPending`
 * and `error` belong to that one press, not to a cache key.
 */
/**
 * The serving parameters an artifact implies, and what the corpus has run.
 *
 * A read of `/artifacts/resolve` against the source the artifact was registered
 * from, so that a form has an answer for every field before anybody types. It is
 * a query rather than the mutation beside it because nothing here is a press: the
 * same weights resolve to the same proposal every time, and it is fetched the
 * moment an artifact is chosen so the defaults are already there.
 *
 * Disabled for an artifact whose bytes came from somewhere the plane cannot go
 * back and ask, which is the same condition the refresh button is disabled on.
 */
export function useArtifactServingDefaults(artifact: Artifact | null) {
  const source = artifact ? artifactSource(artifact) : null
  return useQuery({
    queryKey: ["artifacts", "resolve", source],
    queryFn: () => resolveArtifact(source!),
    enabled: !!source,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })
}

export function useResolveArtifact() {
  return useMutation({
    mutationFn: (source: string) => resolveArtifact(source),
  })
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

/**
 * Re-reads the source and invalidates the fitment along with the artifact.
 *
 * The fitment is the reason anyone presses this: an artifact with no recorded
 * attention shape can only be sized to a floor, and recording one reprices every
 * row in the matrix. Leaving the cached fitment in place would show the old floors
 * next to a freshly shaped artifact, which reads as the refresh having failed.
 */
export function useRefreshArtifact() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => refreshArtifact(ref),
    onSuccess: (result) => {
      client.invalidateQueries({ queryKey: ["artifacts"] })
      client.setQueryData(
        ["artifacts", "detail", result.artifact.id],
        result.artifact
      )
    },
  })
}

export function useDeleteArtifact() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteArtifact(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["artifacts"] })
      client.invalidateQueries({ queryKey: ["workloads"] })
    },
  })
}

/**
 * One artifact's declared quality floors. Drained rather than paged: a set of
 * baselines is a handful of rows and the table shows all of them at once.
 */
export function useArtifactBaselines(ref: string) {
  return useAllPages(
    ["artifacts", "baselines", ref],
    (cursor) => fetchArtifactBaselines(ref, cursor, 200),
    { enabled: !!ref }
  )
}

/**
 * The phases a workload or deployment has stopped moving in.
 *
 * `degraded` is deliberately absent: it is what a rolling update and a crash-looping replica
 * both look like, and both are mid-flight.
 */
const SETTLED_PHASES = new Set(["running", "failed"])

export function useWorkloads(filters: WorkloadFilters = {}) {
  return useCursorList(["workloads", filters], (cursor) =>
    fetchWorkloads(filters, cursor)
  )
}

/**
 * One workload, polled while any of its clusters is still coming up.
 *
 * The rollup on a workload moves for as long as any of its deployments does, so
 * the interval follows the derived phase rather than one cluster's report.
 */
export function useWorkload(ref: string, project?: string) {
  return useQuery({
    queryKey: ["workloads", "detail", ref, project],
    queryFn: () => fetchWorkload(ref, project),
    enabled: !!ref,
    refetchInterval: (query) => {
      const data = query.state.data as Workload | undefined
      if (!data) return POLL_MS
      return SETTLED_PHASES.has(data.phase) ? false : POLL_MS
    },
  })
}

/**
 * Where a workload physically landed, polled while it is still coming up.
 *
 * Placement is observed and moves as allocations are granted and replicas tie
 * to them, so it follows the workload's derived phase the same way the detail
 * query does rather than reading once and sitting on a cache.
 */
export function useWorkloadPlacement(ref: string, project?: string) {
  return useQuery({
    queryKey: ["workloads", "placement", ref, project],
    queryFn: () => fetchWorkloadPlacement(ref, project),
    enabled: !!ref,
    refetchInterval: POLL_MS,
  })
}

/** Every workload, drained — deployments and endpoint targets resolve serving through them. */
export function useWorkloadOptions() {
  return useAllPages(["workloads", "options"], (cursor) =>
    fetchWorkloads({}, cursor, 200)
  )
}

export function useCreateWorkload() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: WorkloadCreateRequest) => createWorkload(body),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["workloads"] })
      client.invalidateQueries({ queryKey: ["deployments"] })
    },
  })
}

export function useUpdateWorkload() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: WorkloadUpdateRequest
      revision?: string
    }) => updateWorkload(vars.ref, vars.body, vars.revision),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["workloads"] })
      client.invalidateQueries({ queryKey: ["deployments"] })
    },
  })
}

/**
 * One workload's serving history, newest first.
 *
 * Not polled. Declared state that only this console's own writes move, and every one of those
 * already invalidates `["workloads"]` — a timer here would re-fetch an append-only list that
 * cannot have changed.
 */
export function useWorkloadRevisions(ref: string, project?: string) {
  return useCursorList(["workloads", "revisions", ref, project], (cursor) =>
    fetchWorkloadRevisions(ref, project, cursor)
  )
}

/**
 * Writes why a configuration was chosen, onto the revision holding it.
 *
 * Invalidates only the history: a note reaches no cluster, moves no digest and changes nothing
 * about the workload itself.
 */
export function useAnnotateWorkloadRevision() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      revision: string
      body: WorkloadRevisionUpdate
      project?: string
    }) =>
      annotateWorkloadRevision(
        vars.ref,
        vars.revision,
        vars.body,
        vars.project
      ),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["workloads", "revisions"] })
    },
  })
}

/** Deleting a workload takes every deployment it expanded into with it. */
export function useDeleteWorkload() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteWorkload(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["workloads"] })
      client.invalidateQueries({ queryKey: ["deployments"] })
      client.invalidateQueries({ queryKey: ["endpoints"] })
    },
  })
}

export function useDeployments(filters: DeploymentFilters = {}) {
  return useCursorList(["deployments", filters], (cursor) =>
    fetchDeployments(filters, cursor)
  )
}

/**
 * One deployment, polled while it is still coming up and left alone once it is not.
 *
 * Without the interval the page reads once and then sits on a cache for as long as it is open,
 * which is exactly the window somebody watching a deploy is looking at it — so a deployment that
 * reached `Serving` a minute ago still showed the `Applied` it was fetched at, and the only way
 * to see the truth was a browser reload. The manual refresh in the header covers the rest of the
 * page; this covers the part that moves on its own.
 */
export function useDeployment(ref: string, cluster?: string) {
  return useQuery({
    queryKey: ["deployments", "detail", ref, cluster],
    queryFn: () => fetchDeployment(ref, cluster),
    enabled: !!ref,
    refetchInterval: (query) => {
      const data = query.state.data as Deployment | undefined
      // No report yet is the least settled a deployment gets, not the most: it means no
      // cluster has said anything, which is the state most worth watching.
      if (!data?.status) return POLL_MS
      return SETTLED_PHASES.has(data.status.phase) ? false : POLL_MS
    },
  })
}

/** Every deployment, drained — endpoint targets reference them by id. */
export function useDeploymentOptions() {
  return useAllPages(["deployments", "options"], (cursor) =>
    fetchDeployments({}, cursor, 200)
  )
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
