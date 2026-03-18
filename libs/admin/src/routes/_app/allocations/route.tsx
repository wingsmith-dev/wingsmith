import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { AllocationsPage } from "./page"

export interface AllocationsSearch {
  cluster?: string
  project?: string
  live?: string
  device?: string
}

export const Route = createFileRoute("/_app/allocations")({
  validateSearch: (search: Record<string, unknown>): AllocationsSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    project: typeof search.project === "string" ? search.project : undefined,
    live: typeof search.live === "string" ? search.live : undefined,
    device: typeof search.device === "string" ? search.device : undefined,
  }),
  component: withPage(AllocationsPage, "/_app/allocations"),
})
