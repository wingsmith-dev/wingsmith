import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { SettingsPage } from "./page"

export const Route = createFileRoute("/_app/settings")({
  component: withPage(SettingsPage, "/_app/settings"),
})
