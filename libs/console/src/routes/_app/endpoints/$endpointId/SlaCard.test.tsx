import { fireEvent, render, screen } from "@/test/render"
import type { SlaReport } from "@/modules/metrics/types"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { SlaCard } from "./SlaCard"

vi.mock("@/modules/metrics/queries", () => ({ useEndpointSla: vi.fn() }))

const { useEndpointSla } = await import("@/modules/metrics/queries")
const mocked = vi.mocked(useEndpointSla)

function report(over: Partial<SlaReport> = {}): SlaReport {
  return {
    endpoint: "end_1",
    endpoint_name: "kimi-k3",
    period_start: "2026-09-01T00:00:00Z",
    period_end: "2026-10-01T00:00:00Z",
    partial: false,
    definition: "availability-2026.1",
    definition_text: "Live traffic only.",
    requests: 9_901_440,
    availability: 99.969,
    failure_ratio: 0.031,
    latency: { p50: 1.42, p95: 6.8, p99: 14.1 },
    ttft: { p50: 0.31, p95: 0.92, p99: 2.4 },
    objective: { success_rate: 0.999, window_seconds: 600, enforced: true },
    met: true,
    error_budget_consumed: 0.31,
    unavailable: null,
    ...over,
  }
}

function answer(over: Record<string, unknown>) {
  mocked.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: null,
    ...over,
  } as unknown as ReturnType<typeof useEndpointSla>)
}

describe("SlaCard", () => {
  beforeEach(() => mocked.mockReset())

  it("puts the availability beside the objective, with the verdict in words", () => {
    answer({ data: report() })
    render(<SlaCard endpointId="end_1" />)
    expect(screen.getByText("99.969%")).toBeInTheDocument()
    expect(screen.getByText("objective 99.900%")).toBeInTheDocument()
    expect(screen.getByText("Met")).toBeInTheDocument()
    expect(
      screen.getByRole("meter", { name: "Error budget used" })
    ).toHaveAttribute("aria-valuetext", "31% used")
    expect(screen.getByText("9.9M")).toBeInTheDocument()
    expect(screen.getByText("14.10 s")).toBeInTheDocument()
  })

  it("draws no budget and no pass for an endpoint nothing was promised on", () => {
    answer({
      data: report({ objective: null, met: null, error_budget_consumed: null }),
    })
    render(<SlaCard endpointId="end_1" />)
    expect(screen.getByText("no objective declared")).toBeInTheDocument()
    expect(screen.getByText("No objective declared")).toBeInTheDocument()
    expect(screen.queryByRole("meter")).not.toBeInTheDocument()
  })

  it("says why there are no figures when the plane could not measure", () => {
    answer({ data: report({ unavailable: "no metrics store for the plane" }) })
    render(<SlaCard endpointId="end_1" />)
    expect(
      screen.getByText("no metrics store for the plane")
    ).toBeInTheDocument()
    expect(screen.queryByText("99.969%")).not.toBeInTheDocument()
  })

  it("holds the card's shape while the report loads", () => {
    answer({ isLoading: true })
    const { container } = render(<SlaCard endpointId="end_1" />)
    expect(screen.getByText("Service level")).toBeInTheDocument()
    expect(
      container.querySelectorAll('[data-slot="skeleton"]').length
    ).toBeGreaterThan(0)
    expect(screen.queryByText("Met")).not.toBeInTheDocument()
  })

  it("says the report failed rather than drawing an empty card", () => {
    answer({ error: new Error("network down") })
    render(<SlaCard endpointId="end_1" />)
    expect(
      screen.getByText(/could not load the report: network down/)
    ).toBeInTheDocument()
  })

  it("asks for the running month first and for a closed one when picked", () => {
    answer({ data: report() })
    render(<SlaCard endpointId="end_1" />)
    const [ref, first, running] = mocked.mock.calls[0]
    expect(ref).toBe("end_1")
    expect(running).toBe(true)

    const buttons = screen.getAllByRole("button")
    expect(buttons[0]).toHaveAttribute("aria-pressed", "true")
    fireEvent.click(buttons[1])
    const [, picked, closed] = mocked.mock.calls.at(-1)!
    expect(picked).not.toBe(first)
    expect(closed).toBe(false)
    expect(buttons[1]).toHaveAttribute("aria-pressed", "true")
  })
})
