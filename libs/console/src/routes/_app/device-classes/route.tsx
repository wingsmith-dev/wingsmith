import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { DeviceClassesPage } from "./page"

export interface DeviceClassesSearch {
  cluster?: string
  architecture?: string
  present?: string
}

export const Route = createFileRoute("/_app/device-classes")({
  validateSearch: (search: Record<string, unknown>): DeviceClassesSearch => ({
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    architecture:
      typeof search.architecture === "string" ? search.architecture : undefined,
    present: typeof search.present === "string" ? search.present : undefined,
  }),
  component: withPage(DeviceClassesPage, "/_app/device-classes"),
})
