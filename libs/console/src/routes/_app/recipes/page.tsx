import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Button } from "@/components/ui/button"
import { StatTile } from "@/routes/_app/overview/StatTile"
import { formatError, formatRelative, shortenId } from "@/lib/format"
import {
  useArtifactOptions,
  useDeleteRecipe,
  useRecipes,
  useRecipeUsages,
} from "@/modules/demand/queries"
import { recipeLifecycleTone } from "@/modules/demand/display"
import type { Recipe } from "@/modules/demand/types"
import { RecipeForm } from "./RecipeForm"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

type Lifecycle = "published" | "draft" | "deprecated"

function columns(
  artifactName: (id: string) => string,
  usage: Map<string, { workload_count: number }>,
  onEdit: (recipe: Recipe) => void,
  onDelete: (recipe: Recipe) => void
): Column<Recipe>[] {
  return [
    {
      key: "name",
      header: "Recipe",
      cell: (recipe) => (
        <ResourceCell kind="recipe">
          <Link
            to="/recipes/$recipeId"
            params={{ recipeId: recipe.id }}
            className="block"
          >
            <span className="font-mono text-xs">{recipe.name}</span>
            <span className="ml-2 font-mono text-2xs text-muted-foreground/60">
              v{recipe.version}
            </span>
            <Id value={shortenId(recipe.id)} className="ml-2" />
          </Link>
        </ResourceCell>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (recipe) => (
        <StatusLabel tone={recipeLifecycleTone(recipe.lifecycle)}>
          {recipe.lifecycle}
        </StatusLabel>
      ),
    },
    {
      key: "artifact",
      header: "Artifact / quantization",
      cell: (recipe) => (
        <div className="flex flex-col gap-0.5">
          <Link
            to="/artifacts/$artifactId"
            params={{ artifactId: recipe.artifact_id }}
            className="font-mono text-xs hover:text-primary"
          >
            {artifactName(recipe.artifact_id)}
          </Link>
          <span className="font-mono text-2xs text-muted-foreground">
            <Dash>{recipe.quantization}</Dash>
            {recipe.dtype && <span> · {recipe.dtype}</span>}
          </span>
        </div>
      ),
    },
    {
      key: "engine",
      header: "Engine",
      cell: (recipe) => (
        <span className="font-mono text-2xs">
          {recipe.engine} {recipe.engine_version}
        </span>
      ),
    },
    {
      key: "hardware",
      header: "Hardware",
      cell: (recipe) => (
        <span className="font-mono text-2xs">
          {recipe.shape.device_count}× {recipe.shape.device_class}
        </span>
      ),
    },
    {
      key: "usedBy",
      header: "Used by",
      cell: (recipe) => {
        const count = usage.get(recipe.id)?.workload_count
        return !count ? (
          <Dash>{null}</Dash>
        ) : (
          <span className="font-mono text-2xs text-muted-foreground">
            {count} workload{count === 1 ? "" : "s"}
          </span>
        )
      },
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      cell: (recipe) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(recipe.updated_at ?? recipe.created_at)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (recipe) => (
        <RowActions
          actions={[
            { label: "Edit", onSelect: () => onEdit(recipe) },
            {
              label: "Delete",
              destructive: true,
              onSelect: () => onDelete(recipe),
            },
          ]}
        />
      ),
    },
  ]
}

const STATUS_TABS: { value: Lifecycle | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "published", label: "Published" },
  { value: "draft", label: "Draft" },
  { value: "deprecated", label: "Deprecated" },
]

/**
 * A shape is a class and a count together — a tensor-parallel-8 recipe means
 * nothing spread across an A100 and an H100, so the two are never split.
 */
export function RecipesPage() {
  const search = useSearch({ from: "/_app/recipes" })
  const navigate = useNavigate({ from: "/recipes" })
  const status = (search.lifecycle as Lifecycle | undefined) ?? "all"
  const all = useRecipes({ artifact: search.artifact })
  const list = useRecipes({
    artifact: search.artifact,
    lifecycle: search.lifecycle,
  })
  const { data: artifacts } = useArtifactOptions()
  const remove = useDeleteRecipe()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Recipe | undefined>()
  const [doomed, setDoomed] = useState<Recipe | undefined>()

  const artifactName = useMemo(() => {
    const byId = new Map((artifacts ?? []).map((a) => [a.id, a.name]))
    return (id: string) => byId.get(id) ?? shortenId(id)
  }, [artifacts])

  const items = useMemo(() => list.items ?? [], [list.items])
  const usage = useRecipeUsages(items.map((recipe) => recipe.id))
  const counts = useMemo(() => {
    const rows = all.items ?? []
    const acc = { total: rows.length, published: 0, draft: 0, deprecated: 0 }
    for (const recipe of rows) {
      if (recipe.lifecycle in acc)
        acc[recipe.lifecycle as Lifecycle] += 1
    }
    return acc
  }, [all.items])

  const setStatus = (value: Lifecycle | "all") =>
    navigate({
      search: (prev) => ({
        ...prev,
        lifecycle: value === "all" ? undefined : value,
      }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Recipes"
        description="declared · a reproducible way to serve an artifact"
        actions={
          <Button onClick={() => setCreating(true)}>
            New recipe
          </Button>
        }
      />
      <div className="space-y-4 p-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile
            kind="recipe"
            label="Recipes"
            value={counts.total}
            to="/recipes"
            isLoading={all.isLoading}
            error={all.error}
          />
          <StatTile
            kind="recipe"
            label="Published"
            value={counts.published}
            to="/recipes"
            isLoading={all.isLoading}
            error={all.error}
          />
          <StatTile
            kind="recipe"
            label="Draft"
            value={counts.draft}
            to="/recipes"
            isLoading={all.isLoading}
            error={all.error}
          />
          <StatTile
            kind="recipe"
            label="Deprecated"
            value={counts.deprecated}
            to="/recipes"
            isLoading={all.isLoading}
            error={all.error}
          />
        </div>

        {search.artifact && (
          <div className="flex items-center gap-2 font-mono text-2xs text-muted-foreground">
            <span>filtered to artifact {search.artifact}</span>
            <Button
              asChild
              size="sm"
              variant="ghost"
              className="font-mono text-2xs"
            >
              <Link to="/recipes" search={{}}>
                clear
              </Link>
            </Button>
          </div>
        )}

        <Tabs
          value={status}
          onValueChange={(value) => setStatus(value as Lifecycle | "all")}
        >
          <TabsList>
            {STATUS_TABS.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value}>
                {tab.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        <DataTable
          columns={columns(artifactName, usage, setEditing, setDoomed)}
          list={list}
          rowKey={(recipe) => recipe.id}
          empty="No recipes defined."
        />
      </div>

      {creating && (
        <RecipeForm open onOpenChange={(open) => !open && setCreating(false)} />
      )}
      {editing && (
        <RecipeForm
          key={editing.revision}
          open
          recipe={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="recipe"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="recipe"
        name={doomed?.name ?? ""}
        note="Deployments built from it are left without a spec."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Recipe deleted", { description: doomed.name })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete recipe")),
          })
        }
      />
    </div>
  )
}
