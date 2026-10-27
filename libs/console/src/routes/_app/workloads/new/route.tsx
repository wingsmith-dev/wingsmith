import { createFileRoute } from "@tanstack/react-router"
import { NewWorkloadPage } from "./page"

/**
 * The flow's state, in the URL, so that "Deploy" on an artifact's fitment row is a link
 * instead of a second copy of this page.
 *
 * Every field is optional and the page opens at whichever step is still unanswered. The
 * footprint travels as its six numbers because there is nothing else to name it by
 * (a serving configuration has no id until the workload that carries it exists), and as
 * search params instead of router state so the link survives a reload and can be
 * pasted to someone.
 */
export interface NewWorkloadSearch {
  artifact?: string
  /** Device class of the chosen fitment row. The other five are meaningless without it. */
  class?: string
  devices?: number
  tp?: number
  pp?: number
  ctx?: number
  quant?: string
}

function int(value: unknown): number | undefined {
  const parsed = typeof value === "string" ? Number(value) : value
  return typeof parsed === "number" && Number.isFinite(parsed) && parsed > 0
    ? parsed
    : undefined
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined
}

export const Route = createFileRoute("/_app/workloads/new")({
  validateSearch: (search: Record<string, unknown>): NewWorkloadSearch => ({
    artifact: str(search.artifact),
    class: str(search.class),
    devices: int(search.devices),
    tp: int(search.tp),
    pp: int(search.pp),
    ctx: int(search.ctx),
    quant: str(search.quant),
  }),
  component: NewWorkloadPage,
})
