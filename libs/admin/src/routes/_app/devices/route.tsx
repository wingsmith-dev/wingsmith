import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { DevicesPage } from "./page"

export interface DevicesSearch {
  cluster?: string
  health?: string
  domain?: string
  allocated?: string
  group?: boolean
}

export const Route = createFileRoute("/_app/devices")({
  validateSearch: (search: Record<string, unknown>): DevicesSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    health: typeof search.health === "string" ? search.health : undefined,
    domain: typeof search.domain === "string" ? search.domain : undefined,
    allocated:
      typeof search.allocated === "string" ? search.allocated : undefined,
    group: search.group === true || search.group === "true" ? true : undefined,
  }),
  component: withPage(DevicesPage, "/_app/devices"),
})
