import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { DeploymentsPage } from "./page"

export interface DeploymentsSearch {
  cluster?: string
  project?: string
  phase?: string
  type?: string
}

export const Route = createFileRoute("/_app/deployments")({
  validateSearch: (search: Record<string, unknown>): DeploymentsSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    project: typeof search.project === "string" ? search.project : undefined,
    phase: typeof search.phase === "string" ? search.phase : undefined,
    type: typeof search.type === "string" ? search.type : undefined,
  }),
  component: withPage(DeploymentsPage, "/_app/deployments"),
})
