import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { NodesPage } from "./page"

export interface NodesSearch {
  cluster?: string
  pool?: string
  condition?: string
}

export const Route = createFileRoute("/_app/nodes")({
  validateSearch: (search: Record<string, unknown>): NodesSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    pool: typeof search.pool === "string" ? search.pool : undefined,
    condition:
      typeof search.condition === "string" ? search.condition : undefined,
  }),
  component: withPage(NodesPage, "/_app/nodes"),
})
