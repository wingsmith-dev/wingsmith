import { createFileRoute } from "@tanstack/react-router"
import { BenchmarkRequestPage } from "./page"

export const Route = createFileRoute("/_app/workbench/requests/$requestId")({
  component: BenchmarkRequestPage,
})
