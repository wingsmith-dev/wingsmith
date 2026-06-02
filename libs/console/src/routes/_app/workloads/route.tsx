import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { WorkloadsPage } from "./page"

export interface WorkloadsSearch {
  project?: string
  kind?: string
  phase?: string
  artifact?: string
  /** Workloads holding a share on this cluster. Global object, per-cluster question. */
  cluster?: string
}

export const Route = createFileRoute("/_app/workloads")({
  validateSearch: (search: Record<string, unknown>): WorkloadsSearch => ({
    project: typeof search.project === "string" ? search.project : undefined,
    kind: typeof search.kind === "string" ? search.kind : undefined,
    phase: typeof search.phase === "string" ? search.phase : undefined,
    artifact: typeof search.artifact === "string" ? search.artifact : undefined,
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
  }),
  component: withPage(WorkloadsPage, "/_app/workloads"),
})
