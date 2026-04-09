import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { parallelismLabel } from "@/modules/demand/display"
import { Button } from "@/components/ui/button"
import { formatError, shortenId } from "@/lib/format"
import { useDeleteRecipe, useRecipes } from "@/modules/demand/queries"
import type { Recipe } from "@/modules/demand/types"
import { RecipeForm } from "./RecipeForm"
import { useState } from "react"
import { toast } from "sonner"
import { Link, useSearch } from "@tanstack/react-router"

function columns(
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
      key: "engine",
      header: "Engine",
      cell: (recipe) => (
        <span className="font-mono text-2xs">
          {recipe.engine} {recipe.engine_version}
        </span>
      ),
    },
    {
      key: "shape",
      header: "Shape",
      cell: (recipe) => (
        <Badge variant="secondary" className="font-mono text-2xs">
          {recipe.shape.device_count}× {recipe.shape.device_class}
        </Badge>
      ),
    },
    {
      key: "parallelism",
      header: "Parallelism",
      cell: (recipe) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {parallelismLabel(recipe.parallelism)}
        </span>
      ),
    },
    {
      key: "context",
      header: "Context",
      align: "right",
      cell: (recipe) => (
        <span className="font-mono text-2xs">
          {recipe.context_length.toLocaleString()}
        </span>
      ),
    },
    {
      key: "quantization",
      header: "Quant / dtype",
      cell: (recipe) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>{recipe.quantization}</Dash> / <Dash>{recipe.dtype}</Dash>
        </span>
      ),
    },
    {
      key: "domain",
      header: "Single domain",
      secondary: true,
      cell: (recipe) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {recipe.shape.requires_single_topology_domain
            ? (recipe.shape.topology_domain_kind ?? "required")
            : "not required"}
        </span>
      ),
    },
    {
      key: "cache",
      header: "Prefix cache",
      secondary: true,
      cell: (recipe) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {recipe.cache.prefix_cache_enabled ? "on" : "off"}
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

/**
 * A shape is a class and a count together — a tensor-parallel-8 recipe means
 * nothing spread across an A100 and an H100, so the two are never split.
 */
export function RecipesPage() {
  const search = useSearch({ from: "/_app/recipes" })
  const list = useRecipes({ artifact: search.artifact })
  const remove = useDeleteRecipe()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Recipe | undefined>()
  const [doomed, setDoomed] = useState<Recipe | undefined>()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Recipes"
        description="declared · a reproducible way to serve an artifact"
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            New recipe
          </Button>
        }
      />
      <div className="space-y-4 p-4">
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
        <DataTable
          columns={columns(setEditing, setDoomed)}
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
