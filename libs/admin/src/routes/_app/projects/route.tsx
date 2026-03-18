import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { ProjectsPage } from "./page"

export const Route = createFileRoute("/_app/projects")({
  component: withPage(ProjectsPage, "/_app/projects"),
})
