import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import { useAllPages, useCursorList } from "@/lib/paginate"
import {
  createArtifact,
  createArtifactBaseline,
  createEndpoint,
  createRecipe,
  createWorkload,
  deleteArtifact,
  deleteArtifactBaseline,
  deleteEndpoint,
  deleteRecipe,
  deleteWorkload,
  fetchArtifact,
  fetchArtifactBaselines,
  fetchArtifactUsage,
  fetchArtifacts,
  fetchDeployment,
  fetchDeployments,
  fetchEndpoint,
  fetchEndpoints,
  fetchRecipe,
  fetchRecipeBenchmarkSummary,
  fetchRecipeUsage,
  fetchRecipes,
  fetchWorkload,
  fetchWorkloadPlacement,
  fetchWorkloads,
  resolveArtifact,
  updateArtifact,
  updateArtifactBaseline,
  updateDeployment,
  updateEndpoint,
  updateRecipe,
  updateWorkload,
  type ArtifactFilters,
  type DeploymentFilters,
  type EndpointFilters,
  type RecipeFilters,
  type WorkloadFilters,
} from "./api"
import type {
  ArtifactBaselineCreateRequest,
  ArtifactBaselineUpdateRequest,
  ArtifactCreateRequest,
  ArtifactUpdateRequest,
  ArtifactUsage,
  Deployment,
  DeploymentUpdateRequest,
  EndpointCreateRequest,
  EndpointUpdateRequest,
  RecipeCreateRequest,
  RecipeUpdateRequest,
  RecipeUsage,
  Workload,
  WorkloadCreateRequest,
  WorkloadUpdateRequest,
} from "./types"

/** Fast enough to watch a deployment come up, slow enough not to poll the plane for sport. */
const POLL_MS = 5000

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
 * One artifact's dependants — recipes, workloads and downstream artifacts —
 * counted at read time by the plane rather than stored on the object.
 */
export function useArtifactUsage(ref: string) {
  return useQuery({
    queryKey: ["artifacts", "usage", ref],
    queryFn: () => fetchArtifactUsage(ref),
    enabled: !!ref,
  })
}

/**
 * Usage for a set of artifacts, one read each. The list surfaces a reference
 * count per row and an in-use total, and the plane counts usage per artifact —
 * there is no batched rollup, so this fans out and reads back a map by id.
 */
export function useArtifactUsages(ids: string[]) {
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: ["artifacts", "usage", id],
      queryFn: () => fetchArtifactUsage(id),
      enabled: !!id,
    })),
  })
  const byId = new Map<string, ArtifactUsage>()
  results.forEach((result, index) => {
    if (result.data) byId.set(ids[index], result.data)
  })
  return byId
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
 * The distinct quantizations declared for each artifact, read from its recipes.
 * Quantization is a recipe property, not an artifact one, so the list column
 * fans out one recipe read per artifact and collapses the set per id.
 */
export function useArtifactQuantizations(ids: string[]) {
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: ["recipes", "by-artifact", id],
      queryFn: () => fetchRecipes({ artifact: id }, undefined, 200),
      enabled: !!id,
    })),
  })
  const byId = new Map<string, string[]>()
  results.forEach((result, index) => {
    const quants = (result.data?.items ?? [])
      .map((recipe) => recipe.quantization)
      .filter((q): q is string => !!q)
    byId.set(ids[index], [...new Set(quants)])
  })
  return byId
}

/** Every artifact, drained — recipes and adapter lists reference them by id. */
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

export function useCreateArtifactBaseline(ref: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: ArtifactBaselineCreateRequest) =>
      createArtifactBaseline(ref, body),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["artifacts", "baselines", ref] }),
  })
}

export function useUpdateArtifactBaseline(ref: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      baseline: string
      body: ArtifactBaselineUpdateRequest
      revision?: string
    }) => updateArtifactBaseline(ref, vars.baseline, vars.body, vars.revision),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["artifacts", "baselines", ref] }),
  })
}

/** Invalidates benchmarks too: a verdict is graded against these rows. */
export function useDeleteArtifactBaseline(ref: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (baseline: string) => deleteArtifactBaseline(ref, baseline),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["artifacts", "baselines", ref] })
      client.invalidateQueries({ queryKey: ["benchmarks"] })
    },
  })
}

export function useRecipes(filters: RecipeFilters = {}) {
  return useCursorList(["recipes", filters], (cursor) =>
    fetchRecipes(filters, cursor)
  )
}

export function useRecipe(ref: string) {
  return useQuery({
    queryKey: ["recipes", "detail", ref],
    queryFn: () => fetchRecipe(ref),
    enabled: !!ref,
  })
}

export function useRecipeOptions() {
  return useAllPages(["recipes", "options"], (cursor) =>
    fetchRecipes({}, cursor, 200)
  )
}

/** How many workloads name this recipe, counted at read time by the plane. */
export function useRecipeUsage(ref: string) {
  return useQuery({
    queryKey: ["recipes", "usage", ref],
    queryFn: () => fetchRecipeUsage(ref),
    enabled: !!ref,
  })
}

/**
 * Usage for a set of recipes, one read each. The list surfaces a workload count
 * per row and the plane counts usage per recipe — there is no batched rollup, so
 * this fans out and reads back a map by id.
 */
export function useRecipeUsages(ids: string[]) {
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: ["recipes", "usage", id],
      queryFn: () => fetchRecipeUsage(id),
      enabled: !!id,
    })),
  })
  const byId = new Map<string, RecipeUsage>()
  results.forEach((result, index) => {
    if (result.data) byId.set(ids[index], result.data)
  })
  return byId
}

/**
 * Offline benchmark rollup for a recipe — peak measured output-token throughput
 * over graded runs. Held-in-plane offline measurement, never live serving telemetry.
 */
export function useRecipeBenchmarkSummary(ref: string) {
  return useQuery({
    queryKey: ["recipes", "benchmarks-summary", ref],
    queryFn: () => fetchRecipeBenchmarkSummary(ref),
    enabled: !!ref,
  })
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
    mutationFn: (vars: {
      ref: string
      body: RecipeUpdateRequest
      revision?: string
    }) => updateRecipe(vars.ref, vars.body, vars.revision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["recipes"] }),
  })
}

export function useDeleteRecipe() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (ref: string) => deleteRecipe(ref),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["recipes"] })
      client.invalidateQueries({ queryKey: ["workloads"] })
    },
  })
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

/** Every workload, drained — deployments and endpoint targets resolve recipe through them. */
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

/** Enabling or disabling one cluster's copy. The only write a deployment takes. */
export function useUpdateDeployment() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (vars: {
      ref: string
      body: DeploymentUpdateRequest
      revision?: string
    }) => updateDeployment(vars.ref, vars.body, vars.revision),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["deployments"] })
      client.invalidateQueries({ queryKey: ["workloads"] })
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
