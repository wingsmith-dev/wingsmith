import { describe, expect, it } from "vitest"
import type { AlertThreshold, MetricRule } from "./types"
import {
  dimensionKey,
  effectiveForSeconds,
  effectiveLookback,
  effectiveSeverity,
  fleetDefaultFor,
  formatSeconds,
  formatThresholdValue,
  overridesFor,
  precedenceNote,
  resolveThreshold,
} from "./display"

// Factories take only what a test asserts on; the rest is filled in as something plausible.
function threshold(over: Partial<AlertThreshold> = {}): AlertThreshold {
  return {
    id: "alt_1",
    rule: "deployment.ttft_p90_high",
    metric: "inference.ttft_p90",
    comparison: "above",
    subject: { type: "deployment", id: null },
    dimensions: {},
    value: 1.5,
    objective: null,
    lookback: null,
    for_seconds: null,
    severity: null,
    enabled: true,
    reason: null,
    created_at: "2026-08-20T10:00:00Z",
    updated_at: null,
    ...over,
  }
}

function rule(over: Partial<MetricRule> = {}): MetricRule {
  return {
    name: "deployment.ttft_p90_high",
    metric: "inference.ttft_p90",
    unit: "seconds",
    comparison: "above",
    subject_type: "deployment",
    severity: "warning",
    measures: "time to first token (p90)",
    suggested_value: 1.5,
    lookback: "5m",
    for_seconds: 900,
    ...over,
  }
}

describe("resolveThreshold", () => {
  it("returns nothing when no row names the rule", () => {
    const resolution = resolveThreshold([], "deployment.ttft_p90_high", "dep_1")
    expect(resolution.applied).toBeNull()
    expect(resolution.inherited).toBe(false)
  })

  it("falls back to the fleet default when the subject has no row", () => {
    const fleet = threshold({ id: "alt_fleet" })
    const resolution = resolveThreshold(
      [fleet],
      "deployment.ttft_p90_high",
      "dep_1"
    )
    expect(resolution.applied?.id).toBe("alt_fleet")
    expect(resolution.inherited).toBe(true)
    expect(resolution.shadowed).toEqual([])
  })

  it("prefers a row about the subject and reports what it shadowed", () => {
    const fleet = threshold({ id: "alt_fleet" })
    const scoped = threshold({
      id: "alt_scoped",
      subject: { type: "deployment", id: "dep_1" },
      value: 4,
    })
    const resolution = resolveThreshold(
      [fleet, scoped],
      "deployment.ttft_p90_high",
      "dep_1"
    )
    expect(resolution.applied?.id).toBe("alt_scoped")
    expect(resolution.inherited).toBe(false)
    expect(resolution.shadowed.map((t) => t.id)).toEqual(["alt_fleet"])
  })

  it("ignores a row about a different subject", () => {
    const other = threshold({
      id: "alt_other",
      subject: { type: "deployment", id: "dep_2" },
    })
    const fleet = threshold({ id: "alt_fleet" })
    const resolution = resolveThreshold(
      [other, fleet],
      "deployment.ttft_p90_high",
      "dep_1"
    )
    expect(resolution.applied?.id).toBe("alt_fleet")
  })

  it("ignores a row about a different rule", () => {
    const elsewhere = threshold({
      id: "alt_latency",
      rule: "deployment.latency_high",
    })
    const resolution = resolveThreshold(
      [elsewhere],
      "deployment.ttft_p90_high",
      "dep_1"
    )
    expect(resolution.applied).toBeNull()
  })

  // The reactor loads enabled rows only, so a switched-off override is not a
  // candidate and the fleet default is what fires. This is the case the screen
  // exists for.
  it("lets the fleet default win over a disabled override, and says so", () => {
    const fleet = threshold({ id: "alt_fleet" })
    const scoped = threshold({
      id: "alt_scoped",
      subject: { type: "deployment", id: "dep_1" },
      value: 4,
      enabled: false,
    })
    const resolution = resolveThreshold(
      [fleet, scoped],
      "deployment.ttft_p90_high",
      "dep_1"
    )
    expect(resolution.applied?.id).toBe("alt_fleet")
    expect(resolution.inherited).toBe(true)
    expect(resolution.ignored.map((t) => t.id)).toEqual(["alt_scoped"])
  })

  it("prefers the least narrowed row at the same scope", () => {
    const narrow = threshold({
      id: "alt_narrow",
      subject: { type: "deployment", id: "dep_1" },
      dimensions: { model: "gpt-20b" },
    })
    const plain = threshold({
      id: "alt_plain",
      subject: { type: "deployment", id: "dep_1" },
    })
    const resolution = resolveThreshold(
      [narrow, plain],
      "deployment.ttft_p90_high",
      "dep_1"
    )
    expect(resolution.applied?.id).toBe("alt_plain")
    expect(resolution.shadowed.map((t) => t.id)).toEqual(["alt_narrow"])
  })

  it("breaks a tie on the dimension key, not on row order", () => {
    const beta = threshold({
      id: "alt_beta",
      subject: { type: "deployment", id: "dep_1" },
      dimensions: { region: "b" },
    })
    const alpha = threshold({
      id: "alt_alpha",
      subject: { type: "deployment", id: "dep_1" },
      dimensions: { region: "a" },
    })
    expect(
      resolveThreshold([beta, alpha], "deployment.ttft_p90_high", "dep_1")
        .applied?.id
    ).toBe("alt_alpha")
    expect(
      resolveThreshold([alpha, beta], "deployment.ttft_p90_high", "dep_1")
        .applied?.id
    ).toBe("alt_alpha")
  })

  it("considers only fleet defaults when the alert names no subject", () => {
    const fleet = threshold({ id: "alt_fleet" })
    const scoped = threshold({
      id: "alt_scoped",
      subject: { type: "deployment", id: "dep_1" },
    })
    const resolution = resolveThreshold(
      [fleet, scoped],
      "deployment.ttft_p90_high",
      null
    )
    expect(resolution.applied?.id).toBe("alt_fleet")
    expect(resolution.inherited).toBe(true)
  })
})

describe("precedenceNote", () => {
  it("counts the subjects that have taken themselves out of a fleet default", () => {
    const fleet = threshold({ id: "alt_fleet" })
    const one = threshold({
      id: "alt_1s",
      subject: { type: "deployment", id: "dep_1" },
    })
    const two = threshold({
      id: "alt_2s",
      subject: { type: "deployment", id: "dep_2" },
    })
    expect(precedenceNote(fleet, [fleet, one, two])).toBe(
      "applies except to 2 overridden subjects"
    )
    expect(precedenceNote(fleet, [fleet])).toBe("applies to every subject")
  })

  it("does not count a disabled override against the fleet default", () => {
    const fleet = threshold({ id: "alt_fleet" })
    const off = threshold({
      id: "alt_off",
      subject: { type: "deployment", id: "dep_1" },
      enabled: false,
    })
    expect(precedenceNote(fleet, [fleet, off])).toBe("applies to every subject")
  })

  it("says a disabled override is not what fires", () => {
    const fleet = threshold({ id: "alt_fleet" })
    const off = threshold({
      id: "alt_off",
      subject: { type: "deployment", id: "dep_1" },
      enabled: false,
    })
    expect(precedenceNote(off, [fleet, off])).toBe(
      "disabled: the fleet default applies"
    )
  })

  it("names an override with no fleet default behind it", () => {
    const only = threshold({
      id: "alt_only",
      subject: { type: "deployment", id: "dep_1" },
    })
    expect(precedenceNote(only, [only])).toBe(
      "the only commitment for this rule"
    )
  })
})

describe("fleetDefaultFor and overridesFor", () => {
  it("splits a rule's rows by scope", () => {
    const fleet = threshold({ id: "alt_fleet" })
    const scoped = threshold({
      id: "alt_scoped",
      subject: { type: "deployment", id: "dep_1" },
    })
    const elsewhere = threshold({
      id: "alt_other_rule",
      rule: "deployment.latency_high",
    })
    const all = [fleet, scoped, elsewhere]
    expect(fleetDefaultFor(all, "deployment.ttft_p90_high")?.id).toBe(
      "alt_fleet"
    )
    expect(
      overridesFor(all, "deployment.ttft_p90_high").map((t) => t.id)
    ).toEqual(["alt_scoped"])
  })
})

describe("inherited rule defaults", () => {
  it("reports a declared window as declared", () => {
    const declared = threshold({ lookback: "30m", for_seconds: 60 })
    expect(effectiveLookback(declared, rule())).toEqual({
      value: "30m",
      fromRule: false,
    })
    expect(effectiveForSeconds(declared, rule())).toEqual({
      value: 60,
      fromRule: false,
    })
  })

  it("falls back to the rule and says the value came from there", () => {
    expect(effectiveLookback(threshold(), rule())).toEqual({
      value: "5m",
      fromRule: true,
    })
    expect(effectiveForSeconds(threshold(), rule())).toEqual({
      value: 900,
      fromRule: true,
    })
    expect(effectiveSeverity(threshold(), rule())).toEqual({
      value: "warning",
      fromRule: true,
    })
  })

  // A zero `for_seconds` is a real commitment (fire on the first tick) and
  // must not be mistaken for an absent one.
  it("keeps a declared zero instead of treating it as unset", () => {
    expect(effectiveForSeconds(threshold({ for_seconds: 0 }), rule())).toEqual({
      value: 0,
      fromRule: false,
    })
  })

  it("returns nothing instead of inventing a default when the rule is unknown", () => {
    expect(effectiveLookback(threshold(), undefined)).toEqual({
      value: null,
      fromRule: true,
    })
  })
})

describe("formatting", () => {
  it("renders a value in the metric's own unit", () => {
    expect(formatThresholdValue(1.5, "seconds")).toBe("1.50 s")
    expect(formatThresholdValue(0.5, "seconds")).toBe("500 ms")
    expect(formatThresholdValue(95, "percent")).toBe("95%")
    expect(formatThresholdValue(0.5, "per_second")).toBe("0.5/s")
    expect(formatThresholdValue(87, "celsius")).toBe("87°C")
  })

  it("renders a hold window the way an operator would write one", () => {
    expect(formatSeconds(0)).toBe("immediately")
    expect(formatSeconds(45)).toBe("45s")
    expect(formatSeconds(900)).toBe("15m")
    expect(formatSeconds(3600)).toBe("1h")
  })

  it("orders dimensions so two renderings of one narrowing match", () => {
    expect(dimensionKey({ b: "2", a: "1" })).toBe("a=1,b=2")
    expect(dimensionKey({})).toBe("")
  })
})
