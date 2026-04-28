import { Id } from "@/components/layout/mono"
import { Badge } from "@/components/ui/badge"
import { formatRelative, shortenId } from "@/lib/format"
import { footprintOfRecipe, footprintSummary } from "@/modules/demand/deploy"
import type { Recipe } from "@/modules/demand/types"

/**
 * Ways we already run these weights, offered above the fitment table.
 *
 * This is where the recipe dropdown went. It used to be the barrier in front of a
 * deploy — an empty select on your first artifact and a dead end. Here it is the
 * shortcut you get *after* naming the artifact, and it is the better answer whenever it
 * is available: an existing recipe has a real id, so placement preview can run against
 * it, and it may carry tuning somebody did by hand that a freshly derived one would not.
 *
 * Deprecated recipes are left out. Somebody retired them on purpose.
 */
export function RecipeReuse({
  recipes,
  onPick,
}: {
  recipes: Recipe[]
  onPick: (recipe: Recipe) => void
}) {
  const usable = recipes.filter((recipe) => recipe.lifecycle !== "deprecated")
  if (usable.length === 0) return null

  return (
    <div className="space-y-1.5">
      <p className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        Run it the way we already run it
      </p>
      <div className="divide-y rounded-lg border">
        {usable.map((recipe) => (
          <button
            key={recipe.id}
            type="button"
            onClick={() => onPick(recipe)}
            className="flex w-full items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-primary/5 focus-visible:bg-primary/5 focus-visible:outline-none"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="truncate font-mono text-xs">
                  {recipe.name}
                </span>
                <Badge variant="outline" className="font-mono text-2xs">
                  v{recipe.version}
                </Badge>
                {recipe.lifecycle === "draft" && (
                  <Badge variant="outline" className="font-mono text-2xs">
                    draft
                  </Badge>
                )}
              </div>
              <span className="font-mono text-2xs text-muted-foreground/60">
                {footprintSummary(footprintOfRecipe(recipe))} · {recipe.engine}{" "}
                {recipe.engine_version}
              </span>
            </div>
            <div className="flex shrink-0 flex-col items-end font-mono text-2xs text-muted-foreground/50">
              <Id value={shortenId(recipe.id)} />
              <span>
                {formatRelative(recipe.updated_at ?? recipe.created_at)}
              </span>
            </div>
          </button>
        ))}
      </div>
      <p className="font-mono text-2xs text-muted-foreground/50">
        Or size a new one against the fleet below.
      </p>
    </div>
  )
}
