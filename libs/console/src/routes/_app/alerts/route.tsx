import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { AlertsPage } from "./page"

export interface AlertsSearch {
  /** `firing` (derived alerts) or `thresholds` (the declared numbers). */
  tab?: string
  state?: string
  severity?: string
  rule?: string
  cluster?: string
  subject?: string
  /** `fleet` or `subject`, which half of the precedence to show. */
  scope?: string
  /** `on` or `off`; absent is both. */
  enabled?: string
}

export const Route = createFileRoute("/_app/alerts")({
  validateSearch: (search: Record<string, unknown>): AlertsSearch => ({
    tab: typeof search.tab === "string" ? search.tab : undefined,
    state: typeof search.state === "string" ? search.state : undefined,
    severity: typeof search.severity === "string" ? search.severity : undefined,
    rule: typeof search.rule === "string" ? search.rule : undefined,
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    subject: typeof search.subject === "string" ? search.subject : undefined,
    scope: typeof search.scope === "string" ? search.scope : undefined,
    enabled: typeof search.enabled === "string" ? search.enabled : undefined,
  }),
  component: withPage(AlertsPage, "/_app/alerts"),
})
