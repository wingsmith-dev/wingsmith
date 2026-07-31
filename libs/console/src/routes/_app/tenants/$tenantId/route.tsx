import { createFileRoute } from "@tanstack/react-router"
import { TenantDetailPage } from "./page"

export const TENANT_TABS = ["overview", "serving", "keys"] as const

export type TenantTab = (typeof TENANT_TABS)[number]

export interface TenantSearch {
  tab: TenantTab
}

function tab(value: unknown): TenantTab {
  return TENANT_TABS.includes(value as TenantTab)
    ? (value as TenantTab)
    : "overview"
}

/**
 * The tab is in the URL rather than in component state, which is a deliberate
 * divergence from the other detail pages.
 *
 * This page exists for a support conversation, and the second thing anybody does
 * with it is send it to somebody else. A tab held in `useState` makes that link
 * land on the overview no matter which tab was being looked at, so the sender has
 * to describe in prose where to click.
 */
export const Route = createFileRoute("/_app/tenants/$tenantId")({
  validateSearch: (search: Record<string, unknown>): TenantSearch => ({
    tab: tab(search.tab),
  }),
  component: TenantDetailPage,
})
