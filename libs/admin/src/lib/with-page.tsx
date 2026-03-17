import { Outlet, useMatches } from "@tanstack/react-router"

/**
 * Wraps a page component so the route renders the page when it is the active
 * leaf, or <Outlet /> when a child route is active.
 */
export function withPage(Page: React.ComponentType, routeId: string) {
  return function RouteComponent() {
    const matches = useMatches()
    if (matches[matches.length - 1].routeId === routeId) return <Page />
    return <Outlet />
  }
}
