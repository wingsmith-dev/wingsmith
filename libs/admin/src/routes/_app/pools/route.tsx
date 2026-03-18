import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { PoolsPage } from "./page"

export interface PoolsSearch {
  cluster?: string
  class?: string
  enabled?: string
}

export const Route = createFileRoute("/_app/pools")({
  validateSearch: (search: Record<string, unknown>): PoolsSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    class: typeof search.class === "string" ? search.class : undefined,
    enabled: typeof search.enabled === "string" ? search.enabled : undefined,
  }),
  component: withPage(PoolsPage, "/_app/pools"),
})
