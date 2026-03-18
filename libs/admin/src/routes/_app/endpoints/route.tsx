import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { EndpointsPage } from "./page"

export interface EndpointsSearch {
  project?: string
  search?: string
}

export const Route = createFileRoute("/_app/endpoints")({
  validateSearch: (search: Record<string, unknown>): EndpointsSearch => ({
    project: typeof search.project === "string" ? search.project : undefined,
    search: typeof search.search === "string" ? search.search : undefined,
  }),
  component: withPage(EndpointsPage, "/_app/endpoints"),
})
