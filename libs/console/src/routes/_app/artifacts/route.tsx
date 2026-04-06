import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { ArtifactsPage } from "./page"

export const Route = createFileRoute("/_app/artifacts")({
  component: withPage(ArtifactsPage, "/_app/artifacts"),
})
