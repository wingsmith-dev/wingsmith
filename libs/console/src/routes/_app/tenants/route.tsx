import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { TenantsPage } from "./page"

export const Route = createFileRoute("/_app/tenants")({
  component: withPage(TenantsPage, "/_app/tenants"),
})
