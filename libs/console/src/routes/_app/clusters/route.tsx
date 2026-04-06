import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { ClustersPage } from "./page"

export interface ClustersSearch {
  region?: string
  phase?: string
  search?: string
}

export const Route = createFileRoute("/_app/clusters")({
  validateSearch: (search: Record<string, unknown>): ClustersSearch => ({
    region: typeof search.region === "string" ? search.region : undefined,
    phase: typeof search.phase === "string" ? search.phase : undefined,
    search: typeof search.search === "string" ? search.search : undefined,
  }),
  component: withPage(ClustersPage, "/_app/clusters"),
})
