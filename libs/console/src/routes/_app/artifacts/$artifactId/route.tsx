import { createFileRoute } from "@tanstack/react-router"
import { ArtifactDetailPage } from "./page"

export const Route = createFileRoute("/_app/artifacts/$artifactId")({
  component: ArtifactDetailPage,
})
