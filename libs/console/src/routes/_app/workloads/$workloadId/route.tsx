import { createFileRoute } from "@tanstack/react-router"
import { WorkloadDetailPage } from "./page"

export const Route = createFileRoute("/_app/workloads/$workloadId")({
  component: WorkloadDetailPage,
})
