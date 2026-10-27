import { withPage } from "@/lib/with-page"
import type { RunKind } from "@/modules/benchmarks/kinds"
import { RUN_KINDS } from "@/modules/benchmarks/kinds"
import { createFileRoute } from "@tanstack/react-router"
import { WorkbenchPage } from "./page"

export interface WorkbenchSearch {
  /** The question a run was asking: the page's primary filter. */
  type?: RunKind
  deployment?: string
  cluster?: string
  phase?: string
  /** The ask a run was expanded from, which is how a parent lists its children. */
  request?: string
  /**
   * Shows the invented rows for the kinds whose harnesses do not exist yet.
   *
   * A design switch, not a feature. Three of the four kinds have nothing behind
   * them (no conformance image is built, `agentic` is not registered), so
   * without this their views cannot be opened at all and the layouts cannot be
   * judged. Off by default and never a default anywhere.
   */
  mock?: boolean
}

function asKind(value: unknown): RunKind | undefined {
  return typeof value === "string" && value in RUN_KINDS
    ? (value as RunKind)
    : undefined
}

export const Route = createFileRoute("/_app/workbench")({
  validateSearch: (search: Record<string, unknown>): WorkbenchSearch => ({
    type: asKind(search.type),
    deployment:
      typeof search.deployment === "string" ? search.deployment : undefined,
    cluster: typeof search.cluster === "string" ? search.cluster : undefined,
    phase: typeof search.phase === "string" ? search.phase : undefined,
    request: typeof search.request === "string" ? search.request : undefined,
    mock: search.mock === true || search.mock === "true" ? true : undefined,
  }),
  component: withPage(WorkbenchPage, "/_app/workbench"),
})
