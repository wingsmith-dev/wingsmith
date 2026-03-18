import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { RecipesPage } from "./page"

export interface RecipesSearch {
  artifact?: string
}

export const Route = createFileRoute("/_app/recipes")({
  validateSearch: (search: Record<string, unknown>): RecipesSearch => ({
    artifact: typeof search.artifact === "string" ? search.artifact : undefined,
  }),
  component: withPage(RecipesPage, "/_app/recipes"),
})
