import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { OperationsPage } from "./page"

export interface OperationsSearch {
  cluster?: string
  type?: string
  phase?: string
  target?: string
}

export const Route = createFileRoute("/_app/operations")({
  validateSearch: (search: Record<string, unknown>): OperationsSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    type: typeof search.type === "string" ? search.type : undefined,
    phase: typeof search.phase === "string" ? search.phase : undefined,
    target: typeof search.target === "string" ? search.target : undefined,
  }),
  component: withPage(OperationsPage, "/_app/operations"),
})
