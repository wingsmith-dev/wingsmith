import { describe, expect, it } from "vitest"

import {
  CURVE_LINES,
  curveCeiling,
  curvePath,
  curveSamples,
  curveSegments,
  curveState,
  curveX,
  curveY,
  formatReading,
  kneeIndex,
  kneeLabel,
  kneeTile,
  stepFieldLabel,
} from "./display"
import {
  curveOpen,
  curveProgress,
  latestSteppedRequest,
  reading,
  type BenchmarkCurve,
  type BenchmarkCurvePoint,
  type BenchmarkRun,
} from "./types"

function point(
  step: number,
  readings: Record<string, number | null>,
  over: Partial<BenchmarkCurvePoint> = {}
): BenchmarkCurvePoint {
  return {
    step_index: step,
    step_value: 1 << step,
    run: `bm_${step}`,
    phase: "succeeded",
    discard: false,
    readings: Object.fromEntries(
      Object.entries(readings).map(([id, value]) => [
        id,
        { value, unit: id.endsWith("_ms") ? "ms" : "tokens/s", path: id },
      ])
    ),
    ...over,
  }
}

function curve(
  points: BenchmarkCurvePoint[],
  knee: Partial<BenchmarkCurve["knee"]> = {}
): BenchmarkCurve {
  return {
    request: "bmr_1",
    serving_digest: "sha256:abc",
    step_field: "concurrency",
    criteria_version: "v1",
    points,
    knee: {
      found: false,
      step_index: null,
      step_value: null,
      method: "first-crossing",
      metric: "ttft_p95_ms",
      threshold: 500,
      reason: "every step stayed under 500.0 ms",
      ...knee,
    },
  }
}

function run(over: Partial<BenchmarkRun>): BenchmarkRun {
  return {
    id: "bm_x",
    request_id: null,
    deployment_id: "dep_1",
    cluster_id: "cls_1",
    endpoint_id: null,
    operation_id: null,
    harness: "aiperf",
    harness_version: "0.4",
    image: "aiperf:0.12.0",
    parameters: {},
    step_index: 0,
    vantage: null,
    discard: false,
    requested_by: "usr_1",
    reason: null,
    provenance: {
      model: "m",
      deployment_generation: 1,
      serving_digest: null,
      engine_args: {},
      serving: {},
    },
    phase: "succeeded",
    summary: null,
    artifacts_uri: null,
    log_tail: null,
    error: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: null,
    started_at: null,
    finished_at: null,
    ...over,
  }
}

describe("reading", () => {
  it("answers null for a named miss and for an id nobody resolved", () => {
    const p = point(0, { ttft_p50_ms: null, ttft_p95_ms: 240 })
    expect(reading(p, "ttft_p50_ms")).toBeNull()
    expect(reading(p, "ttft_p95_ms")).toBe(240)
    expect(reading(p, "made_up_ms")).toBeNull()
  })
})

describe("curveSegments", () => {
  it("breaks the line at a named miss instead of drawing through it", () => {
    const samples = curveSamples(
      [
        point(0, { ttft_p50_ms: 100 }),
        point(1, { ttft_p50_ms: 120 }),
        point(2, { ttft_p50_ms: null }),
        point(3, { ttft_p50_ms: 400 }),
      ],
      "ttft_p50_ms"
    )
    const segments = curveSegments(samples)
    expect(segments.map((s) => s.map((sample) => sample.value))).toEqual([
      [100, 120],
      [400],
    ])
  })

  it("never plots a missing reading as a zero", () => {
    const samples = curveSamples(
      [point(0, { ttft_p50_ms: null }), point(1, { ttft_p50_ms: 120 })],
      "ttft_p50_ms"
    )
    expect(
      curveSegments(samples)
        .flat()
        .map((s) => s.value)
    ).toEqual([120])
    expect(
      curveSamples([point(0, { ttft_p50_ms: null })], "ttft_p50_ms")[0]
    ).toMatchObject({ value: null })
  })

  it("keeps a warm-up out of the line without dropping it from the samples", () => {
    const points = [
      point(0, { ttft_p50_ms: 900 }, { discard: true }),
      point(1, { ttft_p50_ms: 100 }),
      point(2, { ttft_p50_ms: 130 }),
    ]
    const samples = curveSamples(points, "ttft_p50_ms")
    expect(samples).toHaveLength(3)
    expect(samples[0].discard).toBe(true)
    const segments = curveSegments(samples)
    expect(segments).toHaveLength(1)
    expect(segments[0].map((s) => s.value)).toEqual([100, 130])
  })

  it("orders by step index and not by the order the plane listed them", () => {
    const samples = curveSamples(
      [point(2, { ttft_p50_ms: 300 }), point(0, { ttft_p50_ms: 100 })],
      "ttft_p50_ms"
    )
    expect(samples.map((s) => s.value)).toEqual([100, 300])
    expect(samples.map((s) => s.index)).toEqual([0, 1])
  })
})

describe("curveCeiling", () => {
  it("is null when nothing on the axis measured", () => {
    const samples = curveSamples(
      [point(0, { ttft_p50_ms: null }), point(1, {})],
      "ttft_p50_ms"
    )
    expect(curveCeiling([samples])).toBeNull()
  })

  it("scales one axis against every series that shares it, warm-ups aside", () => {
    const p50 = curveSamples(
      [point(0, { ttft_p50_ms: 100 }), point(1, { ttft_p50_ms: 200 })],
      "ttft_p50_ms"
    )
    const p95 = curveSamples(
      [
        point(0, { ttft_p95_ms: 300 }),
        point(1, { ttft_p95_ms: 9000 }, { discard: true }),
      ],
      "ttft_p95_ms"
    )
    expect(curveCeiling([p50, p95])).toBeCloseTo(330)
  })
})

describe("plot geometry", () => {
  it("centers a single step instead of pinning it to the left edge", () => {
    expect(curveX(0, 1, 500)).toBe(250)
  })

  it("spreads the steps evenly and anchors the value axis at zero", () => {
    expect(curveX(0, 3, 500)).toBe(0)
    expect(curveX(2, 3, 500)).toBe(500)
    expect(curveY(0, 100, 200)).toBe(200)
    expect(curveY(100, 100, 200)).toBe(0)
  })

  it("draws a segment as its own points and nothing between them", () => {
    const samples = curveSamples(
      [point(0, { ttft_p50_ms: 50 }), point(1, { ttft_p50_ms: 100 })],
      "ttft_p50_ms"
    )
    const path = curvePath(curveSegments(samples)[0], 2, 100, 100, 100)
    expect(path).toBe("0.0,50.0 100.0,0.0")
  })
})

describe("curveProgress", () => {
  it("counts terminal steps out of the counted ones, warm-ups excluded", () => {
    const answer = curveProgress(
      curve([
        point(0, {}, { discard: true }),
        point(1, { ttft_p50_ms: 100 }),
        point(2, {}, { phase: "failed" }),
        point(3, {}, { phase: "running" }),
        point(4, {}, { phase: "pending" }),
      ])
    )
    expect(answer).toEqual({
      done: 2,
      total: 4,
      open: 2,
      failed: 1,
      discarded: 1,
    })
  })

  it("is zero everywhere for a curve nobody has read", () => {
    expect(curveProgress(undefined).total).toBe(0)
    expect(curveOpen(undefined)).toBe(false)
  })

  it("stops being open once every step is terminal", () => {
    expect(curveOpen(curve([point(0, {}, { phase: "running" })]))).toBe(true)
    expect(curveOpen(curve([point(0, {}, { phase: "failed" })]))).toBe(false)
  })

  it("keeps re-reading a request that has not expanded into any step yet", () => {
    expect(curveOpen(curve([]))).toBe(true)
  })
})

describe("curveState", () => {
  it("tells the four not-a-chart answers apart", () => {
    expect(curveState(undefined)).toBe("none")
    expect(curveState(curve([]))).toBe("empty")
    expect(curveState(curve([point(0, {}, { discard: true })]))).toBe("empty")
    expect(
      curveState(
        curve([
          point(0, {}, { phase: "failed" }),
          point(1, {}, { phase: "timed_out" }),
        ])
      )
    ).toBe("all-failed")
    expect(curveState(curve([point(0, { ttft_p50_ms: 1 })]))).toBe("single")
    expect(
      curveState(curve([point(0, { ttft_p50_ms: 1 }), point(1, {})]))
    ).toBe("curve")
  })

  it("is a curve while it is still running, not a failed one", () => {
    expect(
      curveState(
        curve([
          point(0, {}, { phase: "running" }),
          point(1, {}, { phase: "pending" }),
        ])
      )
    ).toBe("curve")
  })
})

describe("kneeTile", () => {
  it("shows the step value the plane placed the knee at", () => {
    const drawn = curve([point(0, {}), point(1, {}), point(2, {})], {
      found: true,
      step_index: 2,
      step_value: 32,
      reason: "ttft_p95_ms crossed 500.0 ms between concurrency 16 and 32",
    })
    expect(kneeTile(drawn)).toEqual({ value: "32", small: false })
  })

  it("says why there is no knee instead of showing a dash", () => {
    expect(kneeTile(curve([point(0, {})])).value).toBe("not found")
    expect(kneeTile(curve([point(0, {}, { phase: "running" })])).value).toBe(
      "still ramping"
    )
    expect(kneeTile(undefined).value).toBe("—")
  })
})

describe("kneeIndex", () => {
  it("marks the point whose step index the plane named, not its position", () => {
    const drawn = curve([point(3, {}), point(1, {}), point(7, {})], {
      found: true,
      step_index: 7,
      step_value: 128,
    })
    expect(kneeIndex(drawn)).toBe(2)
  })

  it("marks the drawn position, which is not the plane's list order", () => {
    const drawn = curve([point(7, {}), point(1, {}), point(3, {})], {
      found: true,
      step_index: 1,
      step_value: 2,
    })
    expect(kneeIndex(drawn)).toBe(0)
  })

  it("marks nothing when no knee was found, and nothing without a curve", () => {
    expect(kneeIndex(curve([point(0, {}), point(1, {})]))).toBe(-1)
    expect(kneeIndex(undefined)).toBe(-1)
  })
})

describe("kneeLabel", () => {
  it("names the load the knee was found at", () => {
    expect(
      kneeLabel(
        curve([point(0, {})], { found: true, step_index: 0, step_value: 32 })
      )
    ).toBe("concurrency 32")
  })

  it("never spells an unresolved load as a number or as the word null", () => {
    const drawn = curve([point(0, {})], {
      found: true,
      step_index: 4,
      step_value: null,
    })
    expect(kneeLabel(drawn)).toBe("step 4, load unresolved")
    expect(kneeTile(drawn)).toEqual({ value: "load unresolved", small: true })
  })

  it("says not found instead of a knee at nothing", () => {
    expect(kneeLabel(curve([point(0, {})]))).toBe("not found")
  })
})

describe("latestSteppedRequest", () => {
  it("finds the newest request whose runs cover more than one step", () => {
    const runs = [
      run({ id: "a", request_id: "bmr_old", step_index: 0, created_at: "1" }),
      run({ id: "b", request_id: "bmr_old", step_index: 1, created_at: "2" }),
      run({ id: "c", request_id: "bmr_new", step_index: 0, created_at: "8" }),
      run({ id: "d", request_id: "bmr_new", step_index: 1, created_at: "9" }),
    ]
    expect(latestSteppedRequest(runs)).toBe("bmr_new")
  })

  it("ignores single runs and runs that belong to no request", () => {
    expect(
      latestSteppedRequest([
        run({ id: "a", request_id: null, created_at: "9" }),
        run({ id: "b", request_id: "bmr_1", step_index: 0, created_at: "8" }),
      ])
    ).toBeNull()
    expect(latestSteppedRequest(undefined)).toBeNull()
  })

  it("dates a request by its newest run, since a fan-out does not start at once", () => {
    const runs = [
      run({ id: "a", request_id: "bmr_1", step_index: 0, created_at: "1" }),
      run({ id: "b", request_id: "bmr_1", step_index: 1, created_at: "9" }),
      run({ id: "c", request_id: "bmr_2", step_index: 0, created_at: "5" }),
      run({ id: "d", request_id: "bmr_2", step_index: 1, created_at: "6" }),
    ]
    expect(latestSteppedRequest(runs)).toBe("bmr_1")
  })
})

describe("labels and readings", () => {
  it("labels the axis from the field the request walked", () => {
    expect(stepFieldLabel("concurrency")).toBe("Concurrency")
    expect(stepFieldLabel("input_tokens_mean")).toBe("Input tokens")
    expect(stepFieldLabel("batch_shape")).toBe("batch shape")
  })

  it("renders a miss as an em dash and a value in the unit the plane quoted", () => {
    expect(formatReading(null, "ms")).toBe("—")
    expect(formatReading(240.9, "ms")).toBe("240.9 ms")
    expect(formatReading(2210, "ms")).toBe("2.21 s")
    expect(formatReading(87.1, "tokens/s")).toBe("87.1 tokens/s")
  })

  it("keeps the aggregate rate marked as a number no verdict may read", () => {
    const aggregate = CURVE_LINES.find((m) => m.id === "output_tps_total")
    expect(aggregate?.display).toBe(true)
    expect(
      CURVE_LINES.filter((m) => m.axis === "latency").every((m) => !m.display)
    ).toBe(true)
  })
})
