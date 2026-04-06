import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { QuotasPage } from "./page"

export interface QuotasSearch {
  project?: string
  cluster?: string
  class?: string
}

export const Route = createFileRoute("/_app/quotas")({
  validateSearch: (search: Record<string, unknown>): QuotasSearch => ({
    project: typeof search.project === "string" ? search.project : undefined,
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    class: typeof search.class === "string" ? search.class : undefined,
  }),
  component: withPage(QuotasPage, "/_app/quotas"),
})
