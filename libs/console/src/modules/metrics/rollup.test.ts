import { describe, expect, it } from "vitest"

import {
  availabilityDays,
  idleGpuHours,
  latencyStages,
  meanUtilization,
  sumCurvePoints,
  sumCurves,
  utilizationWeek,
} from "./rollup"
import type { MetricSample, Metrics, MetricSeries } from "./types"

function curve(
  metric: string,
  points: [number, number][],
  labels: Record<string, string> = {}
): MetricSeries {
  return {
    metric,
    title: metric,
    unit: "percent",
    labels,
    points: points.map(([at, value]) => ({ at, value })),
  }
}

function answer(over: Partial<Metrics> = {}): Metrics {
  return {
    scope: "workload",
    ref: "wkl_1",
    cluster: null,
    source: null,
    window: "5m",
    samples: [],
    breakdown: [],
    series: [],
    unavailable: null,
    ...over,
  }
}

function sample(metric: string): MetricSample {
  return { metric, title: metric, unit: "seconds", value: 0.4 }
}

/** Local Monday 09:00, because the grid buckets by the reader's own clock. */
const MONDAY_09 = new Date(2026, 7, 24, 9).getTime() / 1000
const HOUR_OF_DAY = 9
const HOUR = 3600

describe("sumCurves", () => {
  it("adds two clusters' rates at the timestamps they share", () => {
    const series = [
      curve("tok", [
        [10, 100],
        [20, 120],
      ]),
      curve("tok", [
        [10, 40],
        [20, 60],
      ]),
    ]
    expect(sumCurves(series, "tok")).toEqual([140, 180])
  })

  it("orders by time however the curves arrived", () => {
    const series = [
      curve("tok", [
        [30, 3],
        [10, 1],
        [20, 2],
      ]),
    ]
    expect(sumCurves(series, "tok")).toEqual([1, 2, 3])
  })

  it("counts a bucket only the clusters that reported it", () => {
    const series = [curve("tok", [[10, 5]]), curve("tok", [[20, 7]])]
    // Not [5, 12] and not [12, 12]: neither cluster reported the other's hour.
    expect(sumCurves(series, "tok")).toEqual([5, 7])
  })

  it("keeps the timestamps, so a chart can carry an axis", () => {
    const series = [curve("tok", [[20, 2]]), curve("tok", [[10, 1]])]
    expect(sumCurvePoints(series, "tok")).toEqual([
      { at: 10, value: 1 },
      { at: 20, value: 2 },
    ])
  })

  it("ignores every other metric, and answers nothing for one absent", () => {
    const series = [curve("tok", [[10, 5]]), curve("other", [[10, 900]])]
    expect(sumCurves(series, "tok")).toEqual([5])
    expect(sumCurves(series, "missing")).toEqual([])
  })
})

describe("utilizationWeek", () => {
  it("draws one grid per cluster and never averages them into one", () => {
    const data = answer({
      series: [
        curve("gpu.utilization", [[MONDAY_09, 90]], { cluster: "us-1" }),
        curve("gpu.utilization", [[MONDAY_09, 10]], { cluster: "ap-1" }),
      ],
    })
    const weeks = utilizationWeek(data)
    expect(weeks.map((w) => w.label)).toEqual(["us-1", "ap-1"])
    // 50 is the mean of 90 and 10 and is true of no card either cluster holds.
    expect(weeks.map((w) => w.grid[0][HOUR_OF_DAY])).toEqual([90, 10])
  })

  it("averages the samples that fall inside one hour", () => {
    const data = answer({
      series: [
        curve(
          "gpu.utilization",
          [
            [MONDAY_09, 60],
            [MONDAY_09 + 60, 80],
          ],
          { cluster: "us-1" }
        ),
      ],
    })
    const hour = HOUR_OF_DAY
    expect(utilizationWeek(data)[0].grid[0][hour]).toBe(70)
  })

  it("leaves an hour nothing reported empty, not idle", () => {
    const data = answer({
      series: [
        curve("gpu.utilization", [[MONDAY_09, 60]], { cluster: "us-1" }),
      ],
    })
    const grid = utilizationWeek(data)[0].grid
    const hour = HOUR_OF_DAY
    expect(grid[0][(hour + 1) % 24]).toBeNull()
    expect(grid[6].every((cell) => cell === null)).toBe(true)
  })

  it("puts Monday first, so the weekend is the last two rows", () => {
    const sunday = MONDAY_09 - 24 * HOUR
    const data = answer({
      series: [curve("gpu.utilization", [[sunday, 42]], { cluster: "us-1" })],
    })
    expect(utilizationWeek(data)[0].grid[6]).toContain(42)
  })

  it("has nothing to draw for an empty or absent answer", () => {
    expect(utilizationWeek(undefined)).toEqual([])
    expect(utilizationWeek(answer())).toEqual([])
    expect(
      utilizationWeek(answer({ series: [curve("gpu.utilization", [])] }))
    ).toEqual([])
  })
})

describe("meanUtilization and idleGpuHours", () => {
  const busy = answer({
    series: [
      curve("gpu.utilization", [
        [MONDAY_09, 50],
        [MONDAY_09 + HOUR, 70],
      ]),
    ],
  })

  it("means every point in the answer", () => {
    expect(meanUtilization(busy)).toBe(60)
    expect(meanUtilization(answer())).toBeNull()
  })

  it("is cards times hours less the share of it they were busy", () => {
    expect(idleGpuHours(busy, 8, 24 * HOUR)).toBeCloseTo(24 * 8 * 0.4)
  })

  it("says nothing when placement holds no cards or nothing was measured", () => {
    expect(idleGpuHours(busy, 0, 24 * HOUR)).toBeNull()
    expect(idleGpuHours(answer(), 8, 24 * HOUR)).toBeNull()
  })
})

describe("latencyStages", () => {
  it("keeps request order instead of the order the samples arrived", () => {
    const stages = latencyStages([
      sample("inference.latency"),
      sample("inference.queue_time"),
      sample("inference.ttft"),
    ])
    expect(stages.map((s) => s.metric)).toEqual([
      "inference.queue_time",
      "inference.ttft",
      "inference.latency",
    ])
  })

  it("gives no row to a stage the store did not answer", () => {
    expect(latencyStages([])).toEqual([])
    expect(latencyStages([sample("gpu.utilization")])).toEqual([])
  })
})

describe("availabilityDays", () => {
  /** Local midnight today, which is the cell the last point lands in. */
  function midnight(daysAgo: number): number {
    const at = new Date()
    at.setHours(0, 0, 0, 0)
    return Math.floor(at.getTime() / 1000) - daysAgo * 86400
  }

  it("always draws the whole strip, so unmeasured days stay visible", () => {
    const days = availabilityDays(answer(), 14)
    expect(days).toHaveLength(14)
    expect(days.every((day) => day.value === null)).toBe(true)
  })

  it("means the hours inside a day instead of taking the last one", () => {
    const today = midnight(0)
    const days = availabilityDays(
      answer({
        series: [
          curve("serving.availability", [
            [today + 3600, 100],
            [today + 7200, 98],
          ]),
        ],
      }),
      3
    )
    expect(days[2].value).toBe(99)
  })

  it("puts each point in its own day, oldest cell first", () => {
    const days = availabilityDays(
      answer({
        series: [
          curve("serving.availability", [
            [midnight(2) + 60, 90],
            [midnight(0) + 60, 100],
          ]),
        ],
      }),
      3
    )
    expect(days.map((day) => day.value)).toEqual([90, null, 100])
  })

  it("reads no availability off a metric that is not one", () => {
    const days = availabilityDays(
      answer({ series: [curve("gpu.utilization", [[midnight(0) + 60, 40]])] }),
      2
    )
    expect(days.map((day) => day.value)).toEqual([null, null])
  })
})
