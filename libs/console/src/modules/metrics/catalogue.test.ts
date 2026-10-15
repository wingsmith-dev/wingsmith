import { describe, expect, it } from "vitest"

import {
  CATALOGUE,
  DEFAULT_TILES,
  metricDefinition,
  metricGroups,
} from "./catalogue"
import { formatMetric } from "./format"

/** The names the plane registered for these two, verbatim. */
const AGGREGATE = "inference.output_tokens_per_second"
const PER_STREAM = "inference.output_tokens_per_stream"

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
      "inference.tpot_p50",
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
      "serving.ttft_p99",
      "serving.failure_ratio",
      "serving.availability",
      "serving.requests_in_flight",
      "serving.input_tokens_per_second",
      "serving.output_tokens_per_second",
      "serving.cache_hit_ratio",
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

  it("marks quantiles and per-request distributions as not adding up", () => {
    for (const name of [
      "inference.ttft_p50",
      "inference.ttft_p90",
      "inference.queue_time",
      "inference.prompt_tokens_per_request",
      "inference.generation_tokens_per_request",
    ]) {
      expect(metricDefinition(name)!.scale, name).not.toBe("aggregate")
    }
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
      "serving.ttft_p99",
      "serving.requests_per_second",
      "serving.requests_in_flight",
      "serving.input_tokens_per_second",
      "serving.output_tokens_per_second",
      "serving.cache_hit_ratio",
    ]) {
      expect(metricDefinition(name)!.title, name).toMatch(/front door/i)
    }
  })

  it("marks the rates as adding up and the quantiles as not", () => {
    expect(metricDefinition("serving.requests_per_second")!.scale).toBe(
      "aggregate"
    )
    expect(metricDefinition("serving.requests_in_flight")!.scale).toBe(
      "aggregate"
    )
    for (const name of [
      "serving.latency_p99",
      "serving.availability",
      "serving.failure_ratio",
    ]) {
      expect(metricDefinition(name)!.scale, name).not.toBe("aggregate")
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
