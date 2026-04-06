import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { DevicesPage } from "./page"

export interface DevicesSearch {
  cluster?: string
  node?: string
  pool?: string
  class?: string
  health?: string
  domain?: string
  allocatable?: string
  group?: boolean
}

export const Route = createFileRoute("/_app/devices")({
  validateSearch: (search: Record<string, unknown>): DevicesSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    node: typeof search.node === "string" ? search.node : undefined,
    pool: typeof search.pool === "string" ? search.pool : undefined,
    class: typeof search.class === "string" ? search.class : undefined,
    health: typeof search.health === "string" ? search.health : undefined,
    domain: typeof search.domain === "string" ? search.domain : undefined,
    allocatable:
      typeof search.allocatable === "string" ? search.allocatable : undefined,
    group: search.group === true || search.group === "true" ? true : undefined,
  }),
  component: withPage(DevicesPage, "/_app/devices"),
})
