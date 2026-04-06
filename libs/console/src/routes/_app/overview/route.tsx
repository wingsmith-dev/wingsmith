import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { OverviewPage } from "./page"

export const Route = createFileRoute("/_app/overview")({
  component: withPage(OverviewPage, "/_app/overview"),
})
