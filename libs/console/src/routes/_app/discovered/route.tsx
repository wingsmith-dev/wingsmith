import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { DiscoveredPage } from "./page"

export interface DiscoveredSearch {
  cluster?: string
  namespace?: string
  status?: string
  search?: string
}

export const Route = createFileRoute("/_app/discovered")({
  validateSearch: (search: Record<string, unknown>): DiscoveredSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    namespace:
      typeof search.namespace === "string" ? search.namespace : undefined,
    status: typeof search.status === "string" ? search.status : undefined,
    search: typeof search.search === "string" ? search.search : undefined,
  }),
  component: withPage(DiscoveredPage, "/_app/discovered"),
})
