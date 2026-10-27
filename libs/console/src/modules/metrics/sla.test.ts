import { describe, expect, it } from "vitest"

import {
  budgetReading,
  compactCount,
  objectivePercent,
  slaMonths,
  slaPercent,
  slaVerdict,
} from "./sla"
import type { SlaReport } from "./types"

function report(over: Partial<SlaReport> = {}): SlaReport {
  return {
    endpoint: "end_1",
    endpoint_name: "kimi-k2",
    period_start: "2026-09-01T00:00:00Z",
    period_end: "2026-10-01T00:00:00Z",
    partial: false,
    definition: "availability-2026.1",
    definition_text: "",
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

describe("slaMonths", () => {
  it("names the running month and the two before it, in UTC", () => {
    // 22:00 on 31 October in São Paulo is already November in UTC.
    const months = slaMonths(new Date("2026-11-01T01:00:00Z"))
    expect(months.map((m) => m.month)).toEqual([
      "2026-11",
      "2026-10",
      "2026-09",
    ])
    expect(months.map((m) => m.running)).toEqual([true, false, false])
    expect(months[0].label).toBe("Nov 2026")
  })

  it("crosses a year boundary and says which year it crossed into", () => {
    const months = slaMonths(new Date("2027-01-15T00:00:00Z"))
    expect(months.map((m) => m.month)).toEqual([
      "2027-01",
      "2026-12",
      "2026-11",
    ])
    expect(months[1].label).toBe("Dec 2026")
  })
})

describe("the verdict", () => {
  it("reads as a word, so it never rests on color alone", () => {
    expect(slaVerdict(report())).toEqual({ label: "Met", tone: "ok" })
    expect(slaVerdict(report({ met: false }))).toEqual({
      label: "Missed",
      tone: "danger",
    })
  })

  it("does not call a month that is still running met or missed", () => {
    expect(slaVerdict(report({ partial: true })).label).toBe("On track")
    expect(slaVerdict(report({ partial: true, met: false })).label).toBe(
      "Over budget so far"
    )
  })

  it("tells no commitment apart from no measurement", () => {
    expect(slaVerdict(report({ objective: null, met: null })).label).toBe(
      "No objective declared"
    )
    expect(
      slaVerdict(
        report({
          objective: {
            success_rate: null,
            window_seconds: 600,
            enforced: false,
          },
          met: null,
        })
      ).label
    ).toBe("No objective declared")
    expect(slaVerdict(report({ met: null })).label).toBe("Not measured")
  })
})

describe("the error budget", () => {
  it("fills the bar to the budget and stops at its end, while the words do not", () => {
    expect(budgetReading(0.31)).toEqual({
      label: "31% used",
      width: 31,
      tone: "ok",
    })
    expect(budgetReading(0.8).tone).toBe("warn")
    expect(budgetReading(3.4)).toEqual({
      label: "340% used",
      width: 100,
      tone: "danger",
    })
  })

  it("draws nothing for a budget nobody measured", () => {
    expect(budgetReading(null)).toEqual({
      label: "—",
      width: 0,
      tone: "default",
    })
  })
})

describe("the figures", () => {
  it("keeps three places, because three nines is argued at the third", () => {
    expect(slaPercent(99.94)).toBe("99.940%")
    expect(slaPercent(99.86)).toBe("99.860%")
    expect(slaPercent(null)).toBe("—")
  })

  it("reads the objective as the percentage it is compared with", () => {
    expect(objectivePercent(report())).toBeCloseTo(99.9)
    expect(objectivePercent(report({ objective: null }))).toBeNull()
  })

  it("compacts a request count", () => {
    expect(compactCount(9_901_440)).toBe("9.9M")
    expect(compactCount(412_000)).toBe("412K")
    expect(compactCount(87)).toBe("87")
    expect(compactCount(null)).toBe("—")
  })
})
