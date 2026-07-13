import { createFileRoute } from "@tanstack/react-router"
import { TenantDetailPage } from "./page"

export const Route = createFileRoute("/_app/tenants/$tenantId")({
  component: TenantDetailPage,
})
