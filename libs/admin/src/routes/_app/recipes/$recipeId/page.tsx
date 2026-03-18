import { Field, FieldGrid } from "@/components/layout/field"
import { Dash, Id, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { DeleteDialog } from "@/components/layout/destructive"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { parallelismLabel } from "@/modules/demand/display"
import { useArtifact, useDeleteRecipe, useRecipe } from "@/modules/demand/queries"
import { RecipeForm } from "../RecipeForm"
import { IconArrowLeft } from "@tabler/icons-react"
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

/**
 * The read shape is nested where the write shape is flat, so the cards follow
 * the response — parallelism, shape, cache and resources each stay together.
 */
export function RecipeDetailPage() {
  const { recipeId } = useParams({ from: "/_app/recipes/$recipeId" })
  const { data: recipe, isLoading, error } = useRecipe(recipeId)
  const { data: artifact } = useArtifact(recipe?.artifact_id ?? "")
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

  return (
    <div className="flex flex-col">
      <PageHeader
        title={recipe?.name ?? "Recipe"}
        description={recipe ? `${recipe.id} · v${recipe.version}` : undefined}
        actions={
          recipe && (
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
                Edit
              </Button>
              <Button size="sm" variant="outline" onClick={() => setDoomed(true)}>
                Delete
              </Button>
            </div>
          )
        }
        readActions={
          <Button asChild variant="outline" size="sm">
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
              onError: (e) => toast.error(formatError(e, "Could not delete recipe")),
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

            <div className="grid gap-4 lg:grid-cols-3">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Identity</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Name">{recipe.name}</Field>
                    <Field label="Version">v{recipe.version}</Field>
                    <Field label="Id" className="sm:col-span-2">
                      <Id value={recipe.id} />
                    </Field>
                    <Field label="Artifact" className="sm:col-span-2">
                      <Link
                        to="/artifacts/$artifactId"
                        params={{ artifactId: recipe.artifact_id }}
                        className="hover:underline"
                      >
                        {artifact?.name ?? recipe.artifact_id}
                      </Link>
                    </Field>
                    <Field label="Revision">{recipe.revision}</Field>
                    <Field label="Created">{formatRelative(recipe.created_at)}</Field>
                    <Field label="Created at">
                      {formatAbsolute(recipe.created_at)}
                    </Field>
                    <Field label="Updated at">
                      {formatAbsolute(recipe.updated_at)}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Engine</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Engine">{recipe.engine}</Field>
                    <Field label="Engine version">{recipe.engine_version}</Field>
                    <Field label="Image" className="sm:col-span-2">
                      <span className="break-all">
                        <Dash>{recipe.engine_image}</Dash>
                      </span>
                    </Field>
                    <Field label="Quantization">
                      <Dash>{recipe.quantization}</Dash>
                    </Field>
                    <Field label="Dtype">
                      <Dash>{recipe.dtype}</Dash>
                    </Field>
                    <Field label="Context length">
                      {recipe.context_length.toLocaleString()}
                    </Field>
                    <Field label="Max batch tokens">
                      <Dash>{recipe.max_batch_tokens?.toLocaleString()}</Dash>
                    </Field>
                    <Field label="Parallelism" className="sm:col-span-2">
                      {parallelismLabel(recipe.parallelism)}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Shape and resources</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Device class">{recipe.shape.device_class}</Field>
                    <Field label="Device count">{recipe.shape.device_count}</Field>
                    <Field label="Device memory min">
                      {recipe.shape.device_memory_mib_min
                        ? `${recipe.shape.device_memory_mib_min.toLocaleString()} MiB`
                        : "unset"}
                    </Field>
                    <Field label="Single domain">
                      {recipe.shape.requires_single_topology_domain
                        ? (recipe.shape.topology_domain_kind ?? "required")
                        : "not required"}
                    </Field>
                    <Field label="CPU cores">
                      <Dash>{recipe.resources.cpu_cores}</Dash>
                    </Field>
                    <Field label="Memory">
                      <Dash>
                        {recipe.resources.memory_mib
                          ? `${recipe.resources.memory_mib.toLocaleString()} MiB`
                          : null}
                      </Dash>
                    </Field>
                    <Field label="Disk" className="sm:col-span-2">
                      <Dash>
                        {recipe.resources.disk_gib
                          ? `${recipe.resources.disk_gib.toLocaleString()} GiB`
                          : null}
                      </Dash>
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Cache</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <FieldGrid>
                  <Field label="KV cache dtype">
                    <Dash>{recipe.cache.kv_cache_dtype}</Dash>
                  </Field>
                  <Field label="KV cache fraction">
                    <Dash>{recipe.cache.kv_cache_fraction}</Dash>
                  </Field>
                  <Field label="Prefix cache" className="sm:col-span-2">
                    {recipe.cache.prefix_cache_enabled ? "on" : "off"}
                  </Field>
                </FieldGrid>
                {Object.keys(recipe.cache.prefix_cache_config).length > 0 && (
                  <Json value={recipe.cache.prefix_cache_config} />
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Engine args</CardTitle>
              </CardHeader>
              <CardContent>
                {Object.keys(recipe.engine_args).length === 0 ? (
                  <p className="font-mono text-2xs text-muted-foreground/50">
                    None — the engine runs on its own defaults.
                  </p>
                ) : (
                  <Json value={recipe.engine_args} />
                )}
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </div>
  )
}
