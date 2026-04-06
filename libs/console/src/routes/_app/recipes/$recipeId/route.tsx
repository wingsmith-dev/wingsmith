import { createFileRoute } from "@tanstack/react-router"
import { RecipeDetailPage } from "./page"

export const Route = createFileRoute("/_app/recipes/$recipeId")({
  component: RecipeDetailPage,
})
