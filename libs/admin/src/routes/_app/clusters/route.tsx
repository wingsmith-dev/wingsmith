import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { ClustersPage } from "./page"

export const Route = createFileRoute("/_app/clusters")({
  component: withPage(ClustersPage, "/_app/clusters"),
})
