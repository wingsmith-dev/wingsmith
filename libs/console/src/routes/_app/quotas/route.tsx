import { withPage } from "@/lib/with-page"
import type { LimitScope } from "@/modules/binding/types"
import { createFileRoute } from "@tanstack/react-router"
import { QuotasPage } from "./page"

export type QuotasTab = "hardware" | "serving"

/**
 * Two unrelated objects share this screen and each keeps its own filters:
 * `project`, `cluster` and `class` narrow devices, `endpoint`, `enforcement`
 * and `scope` narrow traffic. `project` is the one both answer to.
 */
export interface QuotasSearch {
  tab?: QuotasTab
  project?: string
  cluster?: string
  class?: string
  endpoint?: string
  enforcement?: "enforced" | "shadow"
  scope?: LimitScope
}

function tab(value: unknown): QuotasTab | undefined {
  return value === "serving" ? "serving" : undefined
}

function enforcement(value: unknown): QuotasSearch["enforcement"] {
  return value === "enforced" || value === "shadow" ? value : undefined
}

function scope(value: unknown): LimitScope | undefined {
  return value === "per_replica" || value === "divided" ? value : undefined
}

export const Route = createFileRoute("/_app/quotas")({
  validateSearch: (search: Record<string, unknown>): QuotasSearch => ({
    tab: tab(search.tab),
    project: typeof search.project === "string" ? search.project : undefined,
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    class: typeof search.class === "string" ? search.class : undefined,
    endpoint: typeof search.endpoint === "string" ? search.endpoint : undefined,
    enforcement: enforcement(search.enforcement),
    scope: scope(search.scope),
  }),
  component: withPage(QuotasPage, "/_app/quotas"),
})
