import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { PoliciesPage } from "./page"

export const Route = createFileRoute("/_app/policies")({
  component: withPage(PoliciesPage, "/_app/policies"),
})
