import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { PoolsPage } from "./page"

export interface PoolsSearch {
  cluster?: string
}

export const Route = createFileRoute("/_app/pools")({
  validateSearch: (search: Record<string, unknown>): PoolsSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
  }),
  component: withPage(PoolsPage, "/_app/pools"),
})
