import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { RecipesPage } from "./page"

export interface RecipesSearch {
  artifact?: string
  lifecycle?: string
}

export const Route = createFileRoute("/_app/recipes")({
  validateSearch: (search: Record<string, unknown>): RecipesSearch => ({
    artifact: typeof search.artifact === "string" ? search.artifact : undefined,
    lifecycle:
      typeof search.lifecycle === "string" ? search.lifecycle : undefined,
  }),
  component: withPage(RecipesPage, "/_app/recipes"),
})
