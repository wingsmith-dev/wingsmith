import { Field, FieldGrid } from "@/components/layout/field"
import { CopyableId, Dash, Id, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { DeleteDialog } from "@/components/layout/destructive"
import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  formatAbsolute,
  formatError,
  formatRelative,
  shortenId,
} from "@/lib/format"
import {
  emptyCapabilities,
  emptyServing,
  formatBytes,
  parallelismLabel,
  recipeLifecycleTone,
  servingOverrides,
  workloadPhaseTone,
} from "@/modules/demand/display"
import type { Workload } from "@/modules/demand/types"
import {
  useArtifact,
  useDeleteRecipe,
  useRecipe,
  useRecipeBenchmarkSummary,
  useRecipeUsage,
  useWorkloads,
} from "@/modules/demand/queries"
import { ChoiceSet } from "@/components/demand/choice-set"
import { EnforcementBadge } from "@/components/demand/enforcement"
import { Provenance } from "@/components/demand/provenance"
import { RecipeForm } from "../RecipeForm"
import { IconArrowLeft, IconArrowUpRight } from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"

function Json({ value }: { value: unknown }) {
  return (
    <pre className="overflow-x-auto rounded-md border bg-muted/30 p-3 font-mono text-2xs">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

/** One tile in the summary strip at the top of the page. */
function Stat({ label, children }: React.PropsWithChildren<{ label: string }>) {
  return (
    <Card className="p-4">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </div>
      <div className="mt-1.5 font-mono text-lg tabular-nums">{children}</div>
    </Card>
  )
}

/** A right-hand key/value row, mirroring the reference's detail panels. */
function KvRow({
  label,
  children,
}: React.PropsWithChildren<{ label: string }>) {
  return (
    <div className="flex items-start justify-between gap-3 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right font-mono break-words">
        {children}
      </span>
    </div>
  )
}

function usedByColumns(): Column<Workload>[] {
  return [
    {
      key: "name",
      header: "Workload",
      cell: (workload) => (
        <ResourceCell kind="workload">
          <Link
            to="/workloads/$workloadId"
            params={{ workloadId: workload.id }}
            className="inline-flex items-center gap-1 hover:text-primary"
          >
            <span className="font-mono text-xs">{workload.name}</span>
            <IconArrowUpRight className="size-3 text-muted-foreground" />
          </Link>
          <Id value={shortenId(workload.id)} className="ml-2" />
        </ResourceCell>
      ),
    },
    {
      key: "clusters",
      header: "Cluster",
      cell: (workload) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {workload.deployments.length === 0 ? (
            <Dash>{null}</Dash>
          ) : (
            workload.deployments
              .map((d) => d.region ?? d.cluster_name)
              .join(", ")
          )}
        </span>
      ),
    },
    {
      key: "phase",
      header: "Status",
      cell: (workload) => (
        <StatusLabel tone={workloadPhaseTone(workload.phase)}>
          {workload.paused ? "paused" : workload.phase}
        </StatusLabel>
      ),
    },
    {
      key: "scale",
      header: "Replicas",
      align: "right",
      cell: (workload) => (
        <span className="font-mono text-xs tabular-nums">
          {workload.scale.ready}/{workload.scale.desired}
        </span>
      ),
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      secondary: true,
      cell: (workload) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(workload.updated_at ?? workload.created_at)}
        </span>
      ),
    },
  ]
}

/**
 * The read shape is nested where the write shape is flat, so the cards follow
 * the response — parallelism, shape, cache and resources each stay together.
 */
export function RecipeDetailPage() {
  const { recipeId } = useParams({ from: "/_app/recipes/$recipeId" })
  const { data: recipe, isLoading, error } = useRecipe(recipeId)
  const { data: artifact } = useArtifact(recipe?.artifact_id ?? "")
  const usedBy = useWorkloads({ recipe: recipe?.id ?? "" })
  const { data: usage } = useRecipeUsage(recipe?.id ?? "")
  const { data: benchmarks } = useRecipeBenchmarkSummary(recipe?.id ?? "")
  const remove = useDeleteRecipe()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Recipe" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load recipe")}
        </div>
      </div>
    )
  }

  const usedByCount = usage?.workload_count ?? usedBy.items?.length ?? 0
  const peakThroughput = benchmarks?.peak_output_tokens_per_second ?? null
  const lastUpdated = recipe?.updated_at ?? recipe?.created_at ?? null

  const serving = recipe?.serving ?? emptyServing()
  const capabilities = recipe?.capabilities ?? emptyCapabilities()
  const cache = recipe?.cache ?? {
    kv_cache_dtype: null,
    kv_cache_fraction: null,
    prefix_cache_enabled: false,
    prefix_cache_config: {},
  }
  const resolvedArgs = recipe?.resolved_engine_args ?? {}
  const engineArgs = recipe?.engine_args ?? {}
  const overrides = recipe
    ? servingOverrides(recipe.engine, engineArgs, serving)
    : []

  return (
    <div className="flex flex-col">
      <PageHeader
        title={recipe?.name ?? "Recipe"}
        description={recipe ? `${recipe.id} · v${recipe.version}` : undefined}
        actions={
          recipe && (
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setEditing(true)}>
                Edit
              </Button>
              <Button variant="outline" onClick={() => setDoomed(true)}>
                Delete
              </Button>
            </div>
          )
        }
        readActions={
          <Button asChild variant="outline">
            <Link to="/recipes">
              <IconArrowLeft className="size-4" />
              All recipes
            </Link>
          </Button>
        }
      />

      {recipe && editing && (
        <RecipeForm
          key={recipe.revision}
          open
          recipe={recipe}
          onOpenChange={(open) => !open && setEditing(false)}
        />
      )}
      {recipe && (
        <DeleteDialog
          icon="recipe"
          open={doomed}
          onOpenChange={setDoomed}
          kind="recipe"
          name={recipe.name}
          note="Deployments built from it are left without a spec."
          error={remove.error}
          pending={remove.isPending}
          onConfirm={() =>
            remove.mutate(recipe.id, {
              onSuccess: () => {
                setDoomed(false)
                toast.success("Recipe deleted", { description: recipe.name })
                navigate({ to: "/recipes" })
              },
              onError: (e) =>
                toast.error(formatError(e, "Could not delete recipe")),
            })
          }
        />
      )}

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-56 w-full" />}

        {recipe && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <Badge variant="secondary" className="font-mono text-2xs">
                {recipe.shape.device_count}× {recipe.shape.device_class}
              </Badge>
              <span className="font-mono text-2xs text-muted-foreground/60">
                {recipe.engine} {recipe.engine_version} ·{" "}
                {parallelismLabel(recipe.parallelism)}
              </span>
              <Labels labels={recipe.labels} />
            </div>

            {/* Summary strip. Cost estimate has no source yet. */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
              <Stat label="Status">
                <StatusLabel tone={recipeLifecycleTone(recipe.lifecycle)}>
                  {recipe.lifecycle}
                </StatusLabel>
              </Stat>
              <Stat label="Used by">
                {usedByCount} {usedByCount === 1 ? "workload" : "workloads"}
              </Stat>
              <Stat label="Peak throughput tested">
                {peakThroughput === null ? (
                  <Dash>{null}</Dash>
                ) : (
                  <span>
                    {Math.round(peakThroughput).toLocaleString()}{" "}
                    <span className="text-2xs text-muted-foreground">
                      tok/s
                    </span>
                  </span>
                )}
              </Stat>
              <Stat label="Est. cost / hr">
                {/* TODO-API no cost estimate on the recipe */}
                <Dash>{null}</Dash>
              </Stat>
              <Stat label="Last updated">{formatRelative(lastUpdated)}</Stat>
            </div>

            <div className="grid items-start gap-4 lg:grid-cols-3">
              {/* ---- left column: configuration, used-by, history ---- */}
              <div className="space-y-4 lg:col-span-2">
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Model ID</CardTitle>
                    <p className="font-mono text-2xs text-muted-foreground/60">
                      What a client sends in the `model` field of a request. Not
                      the recipe's name — that describes the fit and moves when
                      the fit is redone.
                    </p>
                  </CardHeader>
                  <CardContent>
                    <div className="font-mono text-sm">
                      {recipe.resolved_served_model_name}
                    </div>
                    <p className="mt-1 font-mono text-2xs text-muted-foreground/50">
                      {recipe.served_model_name
                        ? "Declared on this recipe."
                        : "Derived from the artifact. Declare one to override it."}
                    </p>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center gap-2 text-sm">
                      Configuration
                      <EnforcementBadge kind="obeyed" />
                    </CardTitle>
                    <p className="font-mono text-2xs text-muted-foreground/60">
                      Serving engine parameters, resolved as the operator keys
                      them — the declared engine args with the typed serving
                      block laid over the top.
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {Object.keys(resolvedArgs).length === 0 ? (
                      <p className="font-mono text-2xs text-muted-foreground/50">
                        None — the engine runs on its own defaults.
                      </p>
                    ) : (
                      <pre className="overflow-x-auto rounded-lg border bg-background/60 p-4 font-mono text-2xs leading-relaxed">
                        {Object.entries(resolvedArgs)
                          .map(([key, value]) => `${key}: ${value}`)
                          .join("\n")}
                      </pre>
                    )}
                    {overrides.length > 0 && (
                      <p className="text-2xs text-amber-700 dark:text-amber-400">
                        {overrides.map((o) => `--${o.flag}`).join(", ")} came
                        from the typed serving block; the engine args spelling
                        of each is ignored.
                      </p>
                    )}
                    {Object.keys(engineArgs).length > 0 && (
                      <div className="space-y-1.5">
                        <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
                          As declared
                        </div>
                        <p className="font-mono text-2xs text-muted-foreground/50">
                          What the recipe stores, before the serving block is
                          laid over the top.
                        </p>
                        <Json value={engineArgs} />
                      </div>
                    )}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Used by</CardTitle>
                    <p className="font-mono text-2xs text-muted-foreground/60">
                      Workloads currently deploying this recipe
                    </p>
                  </CardHeader>
                  <CardContent>
                    <DataTable
                      columns={usedByColumns()}
                      list={usedBy}
                      rowKey={(w) => w.id}
                      empty="No workloads use this recipe."
                    />
                  </CardContent>
                </Card>

                {/* ---- serving behaviour (obeyed): serving flags + cache ---- */}
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center gap-2 text-sm">
                      Serving
                      <EnforcementBadge kind="obeyed" />
                    </CardTitle>
                    <p className="font-mono text-2xs text-muted-foreground/60">
                      What the served API does and how the KV cache is sized —
                      each field renders to a flag, so a change means a replica
                      restart.
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <FieldGrid>
                      <Field label="Thinking default">
                        {serving.reasoning.default_on ? "on" : "off"}
                      </Field>
                      <Field label="Reasoning parser">
                        <Dash>{serving.reasoning.parser}</Dash>
                      </Field>
                      <Field label="Reasoning transport">
                        {serving.reasoning.transport}
                      </Field>
                      <Field label="Suppress EOS">
                        {serving.reasoning.suppress_eos_until_think_end
                          ? "yes"
                          : "no"}
                      </Field>
                      <Field label="Think begin token">
                        <Dash>{serving.reasoning.think_begin_token}</Dash>
                      </Field>
                      <Field label="Think end token">
                        <Dash>{serving.reasoning.think_end_token}</Dash>
                      </Field>
                      <Field label="Stream usage">{serving.stream_usage}</Field>
                      <Field label="Context overflow">
                        {serving.context_overflow}
                      </Field>
                      <Field label="Tool call parser">
                        <Dash>{serving.tool_call_parser}</Dash>
                      </Field>
                      <Field label="Structured output backend">
                        <Dash>{serving.structured_output_backend}</Dash>
                      </Field>
                      <Field label="Max images per request">
                        <Dash>{serving.max_images_per_request}</Dash>
                      </Field>
                    </FieldGrid>

                    <div className="border-t pt-3">
                      <FieldGrid>
                        <Field label="KV cache dtype">
                          <Dash>{cache.kv_cache_dtype}</Dash>
                        </Field>
                        <Field label="KV cache fraction">
                          <Dash>{cache.kv_cache_fraction}</Dash>
                        </Field>
                        <Field label="Max batch tokens">
                          <Dash>
                            {recipe.max_batch_tokens?.toLocaleString()}
                          </Dash>
                        </Field>
                        <Field label="Prefix cache">
                          {cache.prefix_cache_enabled ? "on" : "off"}
                        </Field>
                      </FieldGrid>
                      {Object.keys(cache.prefix_cache_config ?? {}).length >
                        0 && (
                        <div className="mt-3">
                          <Json value={cache.prefix_cache_config} />
                        </div>
                      )}
                    </div>
                  </CardContent>
                </Card>

                {/* ---- capabilities (asserted): claims, not flags ---- */}
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center gap-2 text-sm">
                      Capabilities
                      <EnforcementBadge kind="asserted" />
                    </CardTitle>
                    <p className="font-mono text-2xs text-muted-foreground/60">
                      What this recipe claims it can do. Nothing here renders or
                      is checked — a conformance run is what proves it wrong.
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <FieldGrid>
                      <Field label="Reasoning">
                        {capabilities.reasoning_supported
                          ? "claimed"
                          : "not claimed"}
                      </Field>
                      <Field label="Streaming">
                        {capabilities.streaming_supported
                          ? "claimed"
                          : "not claimed"}
                      </Field>
                      <Field label="Tool calling">
                        {capabilities.tool_calling_supported
                          ? "claimed"
                          : "not claimed"}
                      </Field>
                      <Field label="Parallel tool calls">
                        {capabilities.parallel_tool_calls
                          ? "claimed"
                          : "not claimed"}
                      </Field>
                      <Field
                        label="Structured output modes"
                        className="sm:col-span-2"
                      >
                        <ChoiceSet
                          value={capabilities.structured_output_modes}
                        />
                      </Field>
                      <Field
                        label="Tool choice modes"
                        className="sm:col-span-2"
                      >
                        <ChoiceSet value={capabilities.tool_choice_modes} />
                      </Field>
                      <Field label="Input modalities" className="sm:col-span-2">
                        <ChoiceSet value={capabilities.input_modalities} />
                      </Field>
                      <Field label="Prompt cache">
                        {capabilities.prompt_cache_mode}
                      </Field>
                      <Field label="Prompt cache TTL">
                        <Dash>{capabilities.prompt_cache_ttl_seconds}</Dash>
                      </Field>
                    </FieldGrid>
                    <Provenance
                      byLabel="Asserted by"
                      claimed
                      value={{
                        by: capabilities.asserted_by,
                        at: capabilities.asserted_at,
                        uri: capabilities.source_uri,
                      }}
                    />
                  </CardContent>
                </Card>

                {/* TODO-API no version history endpoint on the recipe yet */}
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Version history</CardTitle>
                    <p className="font-mono text-2xs text-muted-foreground/60">
                      Changes to this recipe's params, hardware, and artifact
                    </p>
                  </CardHeader>
                  <CardContent>
                    <div className="flex items-center justify-between gap-3 text-xs">
                      <div className="flex items-center gap-2">
                        <Badge className="font-mono text-2xs">
                          v{recipe.version}
                        </Badge>
                        <span className="text-muted-foreground">
                          Current revision
                        </span>
                      </div>
                      <span className="font-mono text-2xs text-muted-foreground">
                        {formatRelative(lastUpdated)}
                      </span>
                    </div>
                    <p className="mt-3 font-mono text-2xs text-muted-foreground/50">
                      Earlier revisions are not retained by the plane.
                    </p>
                  </CardContent>
                </Card>
              </div>

              {/* ---- right column: details, hardware, artifact ---- */}
              <div className="space-y-4">
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Details</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-2.5">
                    <KvRow label="Status">
                      <span className="inline-flex items-center gap-1.5">
                        <StatusDot
                          tone={recipeLifecycleTone(recipe.lifecycle)}
                        />
                        {recipe.lifecycle}
                      </span>
                    </KvRow>
                    <KvRow label="Version">v{recipe.version}</KvRow>
                    <KvRow label="Engine">
                      {recipe.engine} {recipe.engine_version}
                    </KvRow>
                    <KvRow label="Context length">
                      {recipe.context_length.toLocaleString()}
                    </KvRow>
                    <KvRow label="Id">
                      <CopyableId
                        value={recipe.id}
                        className="text-2xs text-muted-foreground/60"
                      />
                    </KvRow>
                    <KvRow label="Created">
                      {formatAbsolute(recipe.created_at)}
                    </KvRow>
                    <KvRow label="Last updated">
                      {formatAbsolute(lastUpdated)}
                    </KvRow>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Hardware</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-2.5">
                    <KvRow label="Device class">
                      {recipe.shape.device_class}
                    </KvRow>
                    <KvRow label="Count">{recipe.shape.device_count}×</KvRow>
                    <KvRow label="Tensor parallel">
                      {recipe.parallelism.tensor}
                    </KvRow>
                    <KvRow label="Pipeline parallel">
                      {recipe.parallelism.pipeline}
                    </KvRow>
                    <KvRow label="Expert parallel">
                      {recipe.parallelism.expert}
                    </KvRow>
                    <KvRow label="Device memory min">
                      {recipe.shape.device_memory_mib_min
                        ? `${recipe.shape.device_memory_mib_min.toLocaleString()} MiB`
                        : "unset"}
                    </KvRow>
                    <KvRow label="Single domain">
                      {recipe.shape.requires_single_topology_domain
                        ? (recipe.shape.topology_domain_kind ?? "required")
                        : "not required"}
                    </KvRow>
                    <KvRow label="CPU cores">
                      <Dash>{recipe.resources.cpu_cores}</Dash>
                    </KvRow>
                    <KvRow label="Memory">
                      <Dash>
                        {recipe.resources.memory_mib
                          ? `${recipe.resources.memory_mib.toLocaleString()} MiB`
                          : null}
                      </Dash>
                    </KvRow>
                    <KvRow label="Disk">
                      <Dash>
                        {recipe.resources.disk_gib
                          ? `${recipe.resources.disk_gib.toLocaleString()} GiB`
                          : null}
                      </Dash>
                    </KvRow>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Artifact</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-2.5">
                    <KvRow label="Artifact">
                      <Link
                        to="/artifacts/$artifactId"
                        params={{ artifactId: recipe.artifact_id }}
                        className="inline-flex items-center gap-1 hover:text-primary hover:underline"
                      >
                        {artifact?.name ?? recipe.artifact_id}
                        <IconArrowUpRight className="size-3 text-muted-foreground" />
                      </Link>
                    </KvRow>
                    <KvRow label="Format">
                      <Dash>{artifact?.format}</Dash>
                    </KvRow>
                    <KvRow label="Quantization">
                      <Dash>{recipe.quantization}</Dash>
                    </KvRow>
                    <KvRow label="Dtype">
                      <Dash>{recipe.dtype}</Dash>
                    </KvRow>
                    <KvRow label="Size on disk">
                      {artifact ? (
                        formatBytes(artifact.size_bytes)
                      ) : (
                        <Dash>{null}</Dash>
                      )}
                    </KvRow>
                    <KvRow label="Fingerprint">
                      <Dash>{artifact?.digest}</Dash>
                    </KvRow>
                  </CardContent>
                </Card>

                {/* TODO-API recipe has no activity/audit feed; single derived row */}
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Activity</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="flex items-start justify-between gap-3 text-xs">
                      <span className="text-muted-foreground">
                        Current revision published
                      </span>
                      <span className="font-mono text-2xs text-muted-foreground">
                        {formatRelative(lastUpdated)}
                      </span>
                    </div>
                    <p className="mt-3 font-mono text-2xs text-muted-foreground/50">
                      The plane keeps no per-revision audit trail for recipes.
                    </p>
                  </CardContent>
                </Card>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
