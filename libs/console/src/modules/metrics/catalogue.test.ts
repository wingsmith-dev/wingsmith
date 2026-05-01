import { describe, expect, it } from "vitest"

import {
  CATALOGUE,
  DEFAULT_TILES,
  isSummable,
  metricDefinition,
  metricGroups,
  metricTitle,
  sumAcross,
} from "./catalogue"
import { formatMetric } from "./format"
import type { MetricSample } from "./types"

/** The names the plane registered for these two, verbatim. */
const AGGREGATE = "inference.output_tokens_per_second"
const PER_STREAM = "inference.output_tokens_per_stream"

function sample(metric: string, value: number | null): MetricSample {
  const definition = metricDefinition(metric)
  return {
    metric,
    title: definition?.title ?? metric,
    unit: definition?.unit ?? "count",
    value,
  }
}

describe("catalogue", () => {
  it("carries every metric the plane's registry serves", () => {
    const expected = [
      "gpu.utilization",
      "gpu.memory_used",
      "gpu.memory_free",
      "gpu.memory_utilization",
      "gpu.temperature",
      "gpu.memory_temperature",
      "gpu.power",
      "gpu.sm_clock",
      "gpu.throttle_reasons",
      "gpu.ecc_uncorrectable",
      "gpu.pcie_replays",
      "inference.ttft",
      "inference.tpot",
      "inference.latency",
      "inference.output_tokens_per_second",
      "inference.input_tokens_per_second",
      "inference.requests_running",
      "inference.requests_waiting",
      "inference.kv_cache_utilization",
      "inference.prefix_cache_hit_rate",
      "inference.ttft_p50",
      "inference.ttft_p90",
      "inference.queue_time",
      "inference.prefill_time",
      "inference.decode_time",
      "inference.output_tokens_per_stream",
      "inference.requests_finished",
      "inference.preemptions",
      "inference.prompt_tokens_per_request",
      "inference.generation_tokens_per_request",
      "serving.requests_per_second",
      "serving.latency_p50",
      "serving.latency_p95",
      "serving.latency_p99",
      "serving.ttft_p50",
      "serving.ttft_p95",
      "serving.failure_ratio",
      "serving.availability",
      "serving.requests_in_flight",
      "serving.input_tokens_per_second",
      "serving.output_tokens_per_second",
    ]
    expect(CATALOGUE.map((metric) => metric.name).sort()).toEqual(
      expected.sort()
    )
  })

  it("gives the ten newly registered metrics the units the plane sends", () => {
    const units: Record<string, string> = {
      "inference.ttft_p50": "seconds",
      "inference.ttft_p90": "seconds",
      "inference.queue_time": "seconds",
      "inference.prefill_time": "seconds",
      "inference.decode_time": "seconds",
      "inference.output_tokens_per_stream": "per_second",
      "inference.requests_finished": "per_second",
      "inference.preemptions": "per_second",
      "inference.prompt_tokens_per_request": "count",
      "inference.generation_tokens_per_request": "count",
    }
    for (const [name, unit] of Object.entries(units)) {
      const definition = metricDefinition(name)
      expect(definition, name).toBeDefined()
      expect(definition!.unit, name).toBe(unit)
      expect(definition!.title.length, name).toBeGreaterThan(0)
    }
  })

  it("names no metric twice", () => {
    const names = CATALOGUE.map((metric) => metric.name)
    expect(new Set(names).size).toBe(names.length)
  })

  it("gives every metric a label no other metric shares", () => {
    const titles = CATALOGUE.map((metric) => metric.title)
    expect(new Set(titles).size).toBe(titles.length)
  })

  it("renders every unit it registers", () => {
    for (const metric of CATALOGUE) {
      expect(formatMetric(1.5, metric.unit), metric.name).not.toBe("—")
    }
  })
})

describe("the two throughput measurements", () => {
  it("are separate entries with labels that say which is which", () => {
    const aggregate = metricDefinition(AGGREGATE)!
    const perStream = metricDefinition(PER_STREAM)!

    expect(aggregate.title).not.toBe(perStream.title)
    expect(aggregate.title).toMatch(/replicas/i)
    expect(perStream.title).toMatch(/stream/i)
    expect(aggregate.scale).toBe("aggregate")
    expect(perStream.scale).toBe("per_stream")
  })

  it("warns, on both, which one an acceptance floor means", () => {
    expect(metricDefinition(AGGREGATE)!.note).toMatch(/not the number/i)
    expect(metricDefinition(PER_STREAM)!.note).toMatch(/30 tokens\/second/i)
  })

  it("sums the aggregate one and refuses to sum the per-stream one", () => {
    const samples = [sample(AGGREGATE, 900), sample(AGGREGATE, 1100)]
    expect(sumAcross(AGGREGATE, samples)).toBe(2000)

    expect(isSummable(AGGREGATE)).toBe(true)
    expect(isSummable(PER_STREAM)).toBe(false)
    expect(() =>
      sumAcross(PER_STREAM, [sample(PER_STREAM, 31), sample(PER_STREAM, 29)])
    ).toThrow(/not summable/)
  })

  it("refuses to sum quantiles and per-request distributions too", () => {
    for (const name of [
      "inference.ttft_p50",
      "inference.ttft_p90",
      "inference.queue_time",
      "inference.prompt_tokens_per_request",
      "inference.generation_tokens_per_request",
    ]) {
      expect(isSummable(name), name).toBe(false)
    }
  })

  it("treats members that reported nothing as absent, not as zero", () => {
    expect(sumAcross(AGGREGATE, [sample(AGGREGATE, null)])).toBeNull()
    expect(sumAcross(AGGREGATE, [])).toBeNull()
    expect(
      sumAcross(AGGREGATE, [sample(AGGREGATE, null), sample(AGGREGATE, 40)])
    ).toBe(40)
  })
})

describe("the three time-to-first-token series", () => {
  it("are three distinct registered names, not one with a percentile knob", () => {
    const names = ["inference.ttft_p50", "inference.ttft_p90", "inference.ttft"]
    const titles = names.map((name) => metricDefinition(name)!.title)
    expect(new Set(titles).size).toBe(3)
    expect(titles).toEqual([
      "Time to first token (p50)",
      "Time to first token (p90)",
      "Time to first token (p95)",
    ])
  })
})

describe("failure rate", () => {
  it("is offered by the gateway family and by no other", () => {
    // A ratio built from an engine exporter's series reports a cluster nothing
    // reaches as healthy, so `gpu` and `inference` must never grow a name that
    // implies one. The gateway counts the requests that reached no cluster,
    // which is why it is the one family allowed to answer this.
    const forbidden = /fail|error|success rate|availab|uptime|refus/i
    for (const metric of CATALOGUE) {
      if (metric.family === "serving") continue
      expect(forbidden.test(metric.name), metric.name).toBe(false)
      expect(forbidden.test(metric.title), metric.title).toBe(false)
    }
    expect(metricDefinition("serving.failure_ratio")!.family).toBe("serving")
    expect(metricDefinition("serving.availability")!.family).toBe("serving")
  })

  it("says so on the completed-requests rate, which is the nearest thing", () => {
    expect(metricDefinition("inference.requests_finished")!.note).toMatch(
      /never reached a cluster/i
    )
  })

  it("says where the gateway's two reliability numbers are counted", () => {
    expect(metricDefinition("serving.failure_ratio")!.note).toMatch(
      /never sees a request that reached no cluster/i
    )
    expect(metricDefinition("serving.availability")!.note).toMatch(
      /no floor of its own/i
    )
  })
})

describe("the serving family", () => {
  it("labels every measurement an engine also reports as the front door's", () => {
    // `serving.latency_p50` and `inference.latency` are different measurements
    // of the same request and the two disagreeing is not a fault. A tile that
    // did not say which one it was would be the fault.
    for (const name of [
      "serving.latency_p50",
      "serving.latency_p95",
      "serving.latency_p99",
      "serving.ttft_p50",
      "serving.ttft_p95",
      "serving.requests_per_second",
      "serving.requests_in_flight",
      "serving.input_tokens_per_second",
      "serving.output_tokens_per_second",
    ]) {
      expect(metricDefinition(name)!.title, name).toMatch(/front door/i)
    }
  })

  it("sums the rates and refuses to sum the quantiles", () => {
    expect(isSummable("serving.requests_per_second")).toBe(true)
    expect(isSummable("serving.requests_in_flight")).toBe(true)
    for (const name of [
      "serving.latency_p99",
      "serving.availability",
      "serving.failure_ratio",
    ]) {
      expect(isSummable(name), name).toBe(false)
    }
  })
})

describe("grouping", () => {
  it("puts every metric in a group its family lists", () => {
    for (const family of ["gpu", "inference", "serving"] as const) {
      const grouped = metricGroups(family).flatMap((group) => group.metrics)
      expect(grouped.length).toBe(
        CATALOGUE.filter((metric) => metric.family === family).length
      )
    }
  })

  it("narrows to a chosen set without reordering it", () => {
    const chosen = [PER_STREAM, "inference.ttft_p50", AGGREGATE]
    const groups = metricGroups("inference", chosen)
    expect(groups.flatMap((group) => group.metrics).map((m) => m.name)).toEqual(
      ["inference.ttft_p50", AGGREGATE, PER_STREAM]
    )
  })

  it("drops groups nothing was chosen from", () => {
    expect(metricGroups("inference", ["inference.ttft"])).toHaveLength(1)
    expect(metricGroups("inference", [])).toHaveLength(0)
  })
})

describe("defaults", () => {
  it("open on names the catalogue knows", () => {
    for (const [family, names] of Object.entries(DEFAULT_TILES)) {
      for (const name of names) {
        expect(metricDefinition(name), name).toBeDefined()
        expect(metricDefinition(name)!.family, name).toBe(family)
      }
    }
  })

  it("chart both throughput measurements, so neither stands in for the other", () => {
    expect(DEFAULT_TILES.inference).toContain(AGGREGATE)
    expect(DEFAULT_TILES.inference).toContain(PER_STREAM)
  })

  it("chart the new latency percentiles beside the p95 one", () => {
    for (const name of [
      "inference.ttft_p50",
      "inference.ttft_p90",
      "inference.queue_time",
    ]) {
      expect(DEFAULT_TILES.inference, name).toContain(name)
    }
  })
})

describe("metricTitle", () => {
  it("prefers the catalogue's label to the one on the wire", () => {
    expect(metricTitle(PER_STREAM, "Output tokens/sec")).toBe(
      "Output tokens/sec, one stream"
    )
  })

  it("keeps a metric the catalogue has never heard of", () => {
    expect(metricTitle("inference.something_new", "Something new")).toBe(
      "Something new"
    )
  })
})
