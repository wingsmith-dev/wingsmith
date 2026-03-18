import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { RecipesPage } from "./page"

export const Route = createFileRoute("/_app/recipes")({
  component: withPage(RecipesPage, "/_app/recipes"),
})
