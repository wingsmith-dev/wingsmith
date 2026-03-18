import { DataTable, type Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { parallelismLabel } from "@/modules/demand/display"
import { useRecipes } from "@/modules/demand/queries"
import type { Recipe } from "@/modules/demand/types"

const columns: Column<Recipe>[] = [
  {
    key: "name",
    header: "Recipe",
    cell: (recipe) => (
      <span className="block">
        <span className="font-mono text-xs">{recipe.name}</span>
        <span className="ml-2 font-mono text-2xs text-muted-foreground/60">
          v{recipe.version}
        </span>
        <Id value={recipe.id} className="ml-2" />
      </span>
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
]

/**
 * A shape is a class and a count together — a tensor-parallel-8 recipe means
 * nothing spread across an A100 and an H100, so the two are never split.
 */
export function RecipesPage() {
  const list = useRecipes()
  return (
    <div className="flex flex-col">
      <PageHeader
        title="Recipes"
        description="declared · a reproducible way to serve an artifact"
      />
      <div className="space-y-4 p-4">
        <DataTable
          columns={columns}
          list={list}
          rowKey={(recipe) => recipe.id}
          empty="No recipes defined."
        />
      </div>
    </div>
  )
}
