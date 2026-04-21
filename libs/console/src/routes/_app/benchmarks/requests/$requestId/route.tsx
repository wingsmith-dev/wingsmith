import { createFileRoute } from "@tanstack/react-router"
import { BenchmarkRequestPage } from "./page"

export const Route = createFileRoute("/_app/benchmarks/requests/$requestId")({
  component: BenchmarkRequestPage,
})
