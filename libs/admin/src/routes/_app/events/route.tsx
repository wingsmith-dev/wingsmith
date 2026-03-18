import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { EventsPage } from "./page"

export interface EventsSearch {
  cluster?: string
  kind?: string
  severity?: string
  subject?: string
}

export const Route = createFileRoute("/_app/events")({
  validateSearch: (search: Record<string, unknown>): EventsSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    kind: typeof search.kind === "string" ? search.kind : undefined,
    severity: typeof search.severity === "string" ? search.severity : undefined,
    subject: typeof search.subject === "string" ? search.subject : undefined,
  }),
  component: withPage(EventsPage, "/_app/events"),
})
