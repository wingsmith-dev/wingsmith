import { createFileRoute } from "@tanstack/react-router"
import { DeploymentDetailPage } from "./page"

export const Route = createFileRoute("/_app/deployments/$deploymentId")({
  component: DeploymentDetailPage,
})
