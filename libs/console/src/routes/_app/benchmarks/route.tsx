import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"
import { BenchmarksPage } from "./page"

export interface BenchmarksSearch {
  deployment?: string
  cluster?: string
  harness?: string
  phase?: string
}

export const Route = createFileRoute("/_app/benchmarks")({
  validateSearch: (search: Record<string, unknown>): BenchmarksSearch => ({
    deployment:
      typeof search.deployment === "string" ? search.deployment : undefined,
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    harness: typeof search.harness === "string" ? search.harness : undefined,
    phase: typeof search.phase === "string" ? search.phase : undefined,
  }),
  component: withPage(BenchmarksPage, "/_app/benchmarks"),
})
