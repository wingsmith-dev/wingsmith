import { createFileRoute } from "@tanstack/react-router"
import { BenchmarkDetailPage } from "./page"

export const Route = createFileRoute("/_app/benchmarks/$benchmarkId")({
  component: BenchmarkDetailPage,
})
