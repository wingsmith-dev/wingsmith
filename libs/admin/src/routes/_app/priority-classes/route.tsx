import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { PriorityClassesPage } from "./page"

export const Route = createFileRoute("/_app/priority-classes")({
  component: withPage(PriorityClassesPage, "/_app/priority-classes"),
})
