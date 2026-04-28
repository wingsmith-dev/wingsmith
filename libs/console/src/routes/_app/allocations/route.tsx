import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { AllocationsPage } from "./page"

/** `live` reads the board; `sandbox` lays a plan over it. */
export type BoardMode = "live" | "sandbox"

export interface AllocationsSearch {
  mode?: BoardMode
  cluster?: string
  /** Device class. The plane's filter is `class`, and so is this. */
  class?: string
  /** Focused on arrival, so a device page can point at its own cell. */
  device?: string
  /** Group clusters under their region. Absent is off — the board starts at the cluster. */
  regions?: true
}

export const Route = createFileRoute("/_app/allocations")({
  validateSearch: (search: Record<string, unknown>): AllocationsSearch => ({
    mode: search.mode === "sandbox" ? "sandbox" : undefined,
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    class: typeof search.class === "string" ? search.class : undefined,
    device: typeof search.device === "string" ? search.device : undefined,
    regions:
      search.regions === true || search.regions === "true" ? true : undefined,
  }),
  component: withPage(AllocationsPage, "/_app/allocations"),
})
