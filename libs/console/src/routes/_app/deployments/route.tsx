import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { DeploymentsPage } from "./page"

/**
 * No `type`: kind is a workload fact and a deployment no longer carries one.
 * `phase` now reads the deployment's own `admission_phase`, which is per-cluster.
 */
export interface DeploymentsSearch {
  cluster?: string
  workload?: string
  project?: string
  phase?: string
}

export const Route = createFileRoute("/_app/deployments")({
  validateSearch: (search: Record<string, unknown>): DeploymentsSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    workload: typeof search.workload === "string" ? search.workload : undefined,
    project: typeof search.project === "string" ? search.project : undefined,
    phase: typeof search.phase === "string" ? search.phase : undefined,
  }),
  component: withPage(DeploymentsPage, "/_app/deployments"),
})
