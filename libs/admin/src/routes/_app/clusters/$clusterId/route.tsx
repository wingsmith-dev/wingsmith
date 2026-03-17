import { createFileRoute } from "@tanstack/react-router"
import { ClusterDetailPage } from "./page"

export const Route = createFileRoute("/_app/clusters/$clusterId")({
  component: ClusterDetailPage,
})
