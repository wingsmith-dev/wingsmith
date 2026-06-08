import { describe, expect, it } from "vitest"
import {
  DAY_POINTS,
  breakEven,
  capacityOf,
  demandUnit,
  economics,
  normaliseCurve,
  paybackDays,
  reference,
  sensitivity,
  summariseDay,
  type EconomicsInputs,
} from "./model"

/**
 * Numbers chosen so every expectation below can be done on paper: one replica
 * decoding 100 tok/s, prefilling ten times that, ten input tokens per output
 * token, one card at a dollar an hour.
 */
function inputs(over: Partial<EconomicsInputs> = {}): EconomicsInputs {
  return {
    inputPricePerMTok: 1,
    outputPricePerMTok: 2,
    cachedInputPricePerMTok: 0.1,
    inputPerOutputToken: 10,
    cacheHitRate: 0,
    outputTokensPerSecond: 100,
    prefillTokensPerSecond: 1000,
    capacitySource: "measured",
    replicas: 1,
    gpuCount: 1,
    spotRatePerGpuHour: 1,
    costPerHourOverride: null,
    utilization: flat(1),
    upfrontCommitment: 0,
    referenceInputPricePerMTok: null,
    referenceOutputPricePerMTok: null,
    ...over,
  }
}

function flat(value: number): number[] {
  return new Array(DAY_POINTS).fill(value)
}

describe("capacityOf", () => {
  it("turns two measured rates into one currency of capacity", () => {
    // Prefill runs 10x decode, so an input token costs a tenth of an output token.
    expect(capacityOf(inputs())).toEqual({ otePerSecond: 100, prefillTax: 0.1 })
  })

  it("scales with replicas, because that is what a replica is", () => {
    expect(capacityOf(inputs({ replicas: 4 })).otePerSecond).toBe(400)
  })

  it("taxes nothing when no prefill rate was given", () => {
    expect(capacityOf(inputs({ prefillTokensPerSecond: 0 })).prefillTax).toBe(0)
  })
})

describe("demandUnit", () => {
  it("prices an output token together with the prompt that earned it", () => {
    const unit = demandUnit(inputs())
    expect(unit.freshInput).toBe(10)
    expect(unit.cachedInput).toBe(0)
    // $2/MTok out + 10 x $1/MTok in.
    expect(unit.revenue).toBeCloseTo(12e-6, 12)
    // One output token, plus ten input tokens at a tenth of one each.
    expect(unit.ote).toBeCloseTo(2, 12)
  })

  it("charges nothing in capacity for a cache hit", () => {
    const unit = demandUnit(inputs({ cacheHitRate: 1 }))
    expect(unit.ote).toBe(1)
    expect(unit.revenue).toBeCloseTo(3e-6, 12)
  })

  it("moves smoothly between the two", () => {
    const unit = demandUnit(inputs({ cacheHitRate: 0.5 }))
    expect(unit.freshInput).toBe(5)
    expect(unit.cachedInput).toBe(5)
    expect(unit.ote).toBeCloseTo(1.5, 12)
  })
})

describe("summariseDay", () => {
  it("integrates a saturated day", () => {
    const day = summariseDay(inputs())
    // 100 OTE/s over a unit costing 2 OTE = 50 output tok/s, x $12/M-unit.
    expect(day.revenuePerDay).toBeCloseTo(51.84, 6)
    expect(day.costPerDay).toBe(24)
    expect(day.marginPerDay).toBeCloseTo(27.84, 6)
    expect(day.marginRatio).toBeCloseTo(27.84 / 51.84, 9)
    expect(day.outputTokensPerDay).toBeCloseTo(4_320_000, 3)
    expect(day.costPerMOutputTokens).toBeCloseTo(24 / 4.32, 9)
  })

  it("counts every token billed, not only the ones decoded", () => {
    const day = summariseDay(inputs())
    expect(day.freshInputTokensPerDay).toBeCloseTo(43_200_000, 3)
    expect(day.cachedInputTokensPerDay).toBe(0)
    // 4.32M out + 43.2M in = 47.52M billed.
    expect(day.costPerMBilledTokens).toBeCloseTo(24 / 47.52, 9)
  })

  it("is the whole point: two days with the same mean earn different money", () => {
    const even = summariseDay(inputs({ utilization: flat(1) }))
    const spiky = summariseDay(
      inputs({
        utilization: Array.from({ length: DAY_POINTS }, (_, i) =>
          i % 2 === 0 ? 2 : 0
        ),
      })
    )
    expect(spiky.meanOffered).toBe(even.meanOffered)
    expect(spiky.revenuePerDay).toBeCloseTo(even.revenuePerDay / 2, 6)
    expect(spiky.clippedRevenuePerDay).toBeCloseTo(even.revenuePerDay / 2, 6)
    expect(spiky.meanServed).toBe(0.5)
  })

  it("has nothing to clip below capacity", () => {
    expect(
      summariseDay(inputs({ utilization: flat(0.5) })).clippedRevenuePerDay
    ).toBe(0)
  })

  it("costs out the cards it held and did not use", () => {
    const day = summariseDay(inputs({ utilization: flat(0.25) }))
    expect(day.idleGpuHoursPerDay).toBeCloseTo(18, 9)
    expect(day.idleCostPerDay).toBeCloseTo(18, 9)
  })
})

describe("breakEven", () => {
  it("solves the flat utilization that pays for an hour", () => {
    // $1/hr of cost against $2.16/hr saturated.
    expect(breakEven(inputs()).flatUtilization).toBeCloseTo(1 / 2.16, 9)
  })

  it("solves the same question against this day's actual shape", () => {
    const answer = breakEven(inputs())
    // A curve that never clips scales linearly, so it lands on the flat answer.
    expect(answer.curveScale).toBeCloseTo(1 / 2.16, 6)
    expect(answer.curveMeanOffered).toBeCloseTo(1 / 2.16, 6)
  })

  it("needs far more demand out of a peaky day, because the peak is thrown away", () => {
    // One two-hour spike at ten times capacity, a flat 20% the rest of the day.
    const spiky = breakEven(
      inputs({
        utilization: Array.from({ length: DAY_POINTS }, (_, i) =>
          i === 0 ? 10 : 0.2
        ),
      })
    )
    // 4.32 x (1 + 11 x 0.2s) = 24 solves at s = 2.07, and the spike is clipped
    // throughout — so the day must offer twice its own capacity on average.
    expect(spiky.curveScale!).toBeCloseTo(2.066, 2)
    expect(spiky.curveMeanOffered!).toBeCloseTo(2.101, 2)
    expect(spiky.curveMeanOffered!).toBeGreaterThan(1 / 2.16)
  })

  it("gives up when no amount of demand covers the cost", () => {
    const answer = breakEven(inputs({ outputTokensPerSecond: 0 }))
    expect(answer.curveScale).toBeNull()
    expect(answer.flatUtilization).toBeNull()
  })

  it("states the volume a day has to produce", () => {
    // $24/day over $12 per million units.
    expect(breakEven(inputs()).outputTokensPerDay).toBeCloseTo(2_000_000, 6)
  })

  it("answers the inverse question, and says when it is already answered", () => {
    // Input tokens alone already cover the day, so output could be given away.
    expect(breakEven(inputs()).outputPricePerMTok).toBeCloseTo(
      24 / 4.32 - 10,
      6
    )
  })

  it("asks a real price of a day that does not cover itself", () => {
    const answer = breakEven(
      inputs({ inputPricePerMTok: 0, utilization: flat(0.2) })
    )
    // 100/2 x 0.2 x 86400 = 864k output tokens for $24.
    expect(answer.outputPricePerMTok).toBeCloseTo((24 / 864_000) * 1e6, 6)
  })
})

describe("degenerate models", () => {
  it("with no capacity, earns nothing and loses the rent", () => {
    const model = economics(inputs({ outputTokensPerSecond: 0 }))
    expect(model.capacity.otePerSecond).toBe(0)
    expect(model.day.revenuePerDay).toBe(0)
    expect(model.day.marginPerDay).toBe(-24)
    expect(model.day.marginRatio).toBeNull()
    expect(model.day.outputTokensPerDay).toBe(0)
    expect(model.day.costPerMOutputTokens).toBeNull()
    expect(model.day.costPerMBilledTokens).toBeNull()
    expect(model.breakEven.flatUtilization).toBeNull()
  })

  it("with no price, earns nothing at any volume", () => {
    const model = economics(
      inputs({
        inputPricePerMTok: 0,
        outputPricePerMTok: 0,
        cachedInputPricePerMTok: 0,
      })
    )
    expect(model.day.revenuePerDay).toBe(0)
    expect(model.revenuePerHourAtCapacity).toBe(0)
    expect(model.breakEven.outputTokensPerDay).toBeNull()
    expect(model.breakEven.flatUtilization).toBeNull()
    expect(model.day.marginPerDay).toBe(-24)
  })

  it("with every input cached, serves twice the tokens for a quarter the money", () => {
    const model = economics(inputs({ cacheHitRate: 1 }))
    expect(model.outputTokensPerSecondAtCapacity).toBe(100)
    expect(model.revenuePerHourAtCapacity).toBeCloseTo(1.08, 9)
    expect(model.day.freshInputTokensPerDay).toBe(0)
    expect(model.day.cachedInputTokensPerDay).toBeCloseTo(86_400_000, 3)
  })

  it("with nothing to pay, is all margin and breaks even at zero", () => {
    const model = economics(inputs({ costPerHourOverride: 0 }))
    expect(model.costPerHour).toBe(0)
    expect(model.day.costPerDay).toBe(0)
    expect(model.day.marginRatio).toBe(1)
    expect(model.breakEven.flatUtilization).toBe(0)
    expect(model.breakEven.curveScale).toBe(0)
    expect(model.breakEven.outputTokensPerDay).toBe(0)
    expect(model.paybackDays).toBeNull()
  })

  it("prefers a stated cost per hour over the derived one", () => {
    expect(economics(inputs({ costPerHourOverride: 7 })).costPerHour).toBe(7)
  })
})

describe("paybackDays", () => {
  it("does not apply to rented capacity", () => {
    expect(paybackDays(inputs())).toBeNull()
  })

  it("divides a commitment by what a day clears", () => {
    expect(paybackDays(inputs({ upfrontCommitment: 2784 }))).toBeCloseTo(100, 6)
  })

  it("refuses to date the recovery of a day that loses money", () => {
    expect(
      paybackDays(inputs({ upfrontCommitment: 1000, utilization: flat(0.1) }))
    ).toBeNull()
  })
})

describe("sensitivity", () => {
  it("ranks the inputs by how far they move the day", () => {
    const rows = sensitivity(inputs())
    expect(rows.map((row) => row.swing)).toEqual(
      [...rows.map((row) => row.swing)].sort((a, b) => b - a)
    )
    // Ten input tokens per output one: the input rate carries the revenue.
    expect(rows[0].key).toBe("inputPricePerMTok")
  })

  it("says how far the spot rate can run before the day stops paying", () => {
    const row = sensitivity(inputs()).find(
      (r) => r.key === "spotRatePerGpuHour"
    )!
    // $51.84/day of revenue over 24 hours on one card.
    expect(row.breakEvenValue).toBeCloseTo(2.16, 3)
    expect(row.headroom).toBeCloseTo(1.16, 3)
  })

  it("says how little demand the day can survive on", () => {
    const row = sensitivity(inputs()).find((r) => r.key === "demand")!
    expect(row.breakEvenValue).toBeCloseTo(1 / 2.16, 2)
  })

  it("reports no swing for a lever sitting at zero, but still finds its floor", () => {
    const row = sensitivity(inputs()).find((r) => r.key === "cacheHitRate")!
    expect(row.current).toBe(0)
    expect(row.swing).toBe(0)
    expect(row.headroom).toBeNull()
  })
})

describe("reference", () => {
  it("says nothing when no public price was given", () => {
    expect(reference(inputs())).toBeNull()
  })

  it("blends both rates over the mix actually served", () => {
    const answer = reference(
      inputs({
        referenceInputPricePerMTok: 2,
        referenceOutputPricePerMTok: 6,
      })
    )!
    expect(answer.ours).toBeCloseTo(12, 9)
    expect(answer.theirs).toBeCloseTo(26, 9)
    expect(answer.delta).toBeCloseTo(-14 / 26, 9)
  })
})

describe("normaliseCurve", () => {
  it("always answers with a whole day", () => {
    expect(normaliseCurve([1, 2])).toHaveLength(DAY_POINTS)
    expect(normaliseCurve(new Array(40).fill(1))).toHaveLength(DAY_POINTS)
  })

  it("reads a missing, negative or unreadable point as no demand", () => {
    expect(normaliseCurve([-1, NaN, Infinity, 0.5])[0]).toBe(0)
    expect(normaliseCurve([-1, NaN, Infinity, 0.5])[1]).toBe(0)
    expect(normaliseCurve([-1, NaN, Infinity, 0.5])[3]).toBe(0.5)
  })
})
