import { render, screen } from "@/test/render"
import { metricDefinition } from "@/modules/metrics/catalog"
import type { Metrics, MetricSample } from "@/modules/metrics/types"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { MetricsPanel } from "./metrics-panel"

vi.mock("@/modules/metrics/queries", () => ({ useMetrics: vi.fn() }))

const { useMetrics } = await import("@/modules/metrics/queries")
const mocked = vi.mocked(useMetrics)

function sample(metric: string, value: number | null): MetricSample {
  const definition = metricDefinition(metric)!
  // The wire carries the plane's own title; the console's label is the one the
  // catalog holds, and these tests assert the second is what gets rendered.
  return {
    metric,
    title: "as the plane spells it",
    unit: definition.unit,
    value,
  }
}

const SERVING = [
  sample("inference.ttft", 0.42),
  sample("inference.ttft_p50", 0.21),
  sample("inference.ttft_p90", 0.38),
  sample("inference.tpot", 0.02),
  sample("inference.queue_time", 0.11),
  sample("inference.latency", 3.2),
  sample("inference.output_tokens_per_second", 1840),
  sample("inference.output_tokens_per_stream", 31.4),
  sample("inference.input_tokens_per_second", 9100),
  sample("inference.requests_finished", 12.5),
  sample("inference.requests_running", 18),
  sample("inference.requests_waiting", null),
  sample("inference.kv_cache_utilization", 62),
]

function metrics(over: Partial<Metrics> = {}): Metrics {
  return {
    scope: "deployment",
    ref: "dep_1",
    cluster: "clu_1",
    source: { cluster: "clu_1", url: "http://prom", kind: "prometheus" },
    window: "5m",
    samples: SERVING,
    breakdown: [],
    series: [],
    unavailable: null,
    ...over,
  }
}

function answer(data: Metrics | undefined, over: Record<string, unknown> = {}) {
  mocked.mockReturnValue({
    data,
    isLoading: false,
    error: null,
    ...over,
  } as unknown as ReturnType<typeof useMetrics>)
}

beforeEach(() => {
  mocked.mockReset()
})

describe("MetricsPanel", () => {
  it("charts the ten newly registered serving metrics", () => {
    answer(metrics())
    render(<MetricsPanel scope="deployment" refId="dep_1" family="inference" />)

    for (const name of [
      "inference.ttft_p50",
      "inference.ttft_p90",
      "inference.queue_time",
      "inference.output_tokens_per_stream",
      "inference.requests_finished",
    ]) {
      expect(
        screen.getByText(metricDefinition(name)!.title),
        name
      ).toBeInTheDocument()
    }
  })

  it("labels the two throughput measurements apart", () => {
    answer(metrics())
    render(<MetricsPanel scope="deployment" refId="dep_1" family="inference" />)

    const aggregate = screen.getByText("Output tokens/sec, all replicas")
    const perStream = screen.getByText("Output tokens/sec, one stream")
    expect(aggregate).toBeInTheDocument()
    expect(perStream).toBeInTheDocument()
    expect(aggregate.title).toMatch(/rises with scale/i)
    expect(perStream.title).toMatch(/never summed/i)

    // Both numbers are on screen as themselves. Neither is the other's total.
    expect(screen.getByText("1840.0/s")).toBeInTheDocument()
    expect(screen.getByText("31.4/s")).toBeInTheDocument()
  })

  it("prefers the catalog's label to the title on the wire", () => {
    answer(metrics())
    render(<MetricsPanel scope="deployment" refId="dep_1" family="inference" />)
    expect(screen.queryByText("as the plane spells it")).not.toBeInTheDocument()
  })

  it("says where an availability number would have to come from", () => {
    answer(metrics())
    render(<MetricsPanel scope="deployment" refId="dep_1" family="inference" />)
    expect(screen.getByText(/gateway's own \/metrics/)).toBeInTheDocument()
    expect(screen.getByText(/would read an outage as no traffic/)).toBeVisible()
  })

  it("charts no failure rate of its own", () => {
    answer(metrics())
    const { container } = render(
      <MetricsPanel scope="deployment" refId="dep_1" family="inference" />
    )
    const tiles = Array.from(container.querySelectorAll("[data-slot=card]"))
    for (const tile of tiles) {
      expect(tile.textContent ?? "").not.toMatch(
        /failure rate|error rate|success rate/i
      )
    }
  })

  it("renders a metric the store had no series for as absent, not as zero", () => {
    answer(metrics())
    render(<MetricsPanel scope="deployment" refId="dep_1" family="inference" />)
    expect(screen.getByText("—")).toBeInTheDocument()
    expect(screen.queryByText("0")).not.toBeInTheDocument()
  })

  it("groups the serving metrics rather than laying nineteen tiles in a row", () => {
    answer(metrics())
    render(<MetricsPanel scope="deployment" refId="dep_1" family="inference" />)
    expect(screen.getByText("Latency")).toBeInTheDocument()
    expect(screen.getByText("Throughput")).toBeInTheDocument()
    expect(screen.getByText("Load")).toBeInTheDocument()
  })

  it("promotes the lead metrics out of their group and does not repeat them", () => {
    answer(metrics())
    render(
      <MetricsPanel
        scope="deployment"
        refId="dep_1"
        family="inference"
        lead={["inference.ttft", "inference.output_tokens_per_stream"]}
      />
    )
    expect(screen.getAllByText("Output tokens/sec, one stream")).toHaveLength(1)
    expect(screen.getAllByText("Time to first token (p95)")).toHaveLength(1)
  })

  it("draws the lead row and nothing else when it is asked for a summary", () => {
    answer(metrics())
    render(
      <MetricsPanel
        scope="deployment"
        refId="dep_1"
        family="inference"
        lead={["inference.ttft", "inference.output_tokens_per_stream"]}
        leadOnly
      />
    )
    expect(screen.getAllByText("Time to first token (p95)")).toHaveLength(1)
    // The group headings, the picker and the store footer are what a summary
    // is a summary of: an overview carrying all of them is the whole page again.
    expect(screen.queryByText("Latency")).not.toBeInTheDocument()
    expect(screen.queryByText("Throughput")).not.toBeInTheDocument()
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })

  it("offers nothing that edits observed measurement", () => {
    answer(metrics())
    render(<MetricsPanel scope="deployment" refId="dep_1" family="inference" />)
    const buttons = screen.getAllByRole("button")
    expect(buttons).toHaveLength(1)
    expect(buttons[0]).toHaveAccessibleName(/metrics/i)
  })

  it("keeps the gateway note off the GPU family, which never claimed one", () => {
    answer(
      metrics({
        samples: [sample("gpu.utilization", 71), sample("gpu.temperature", 88)],
        scope: "node",
      })
    )
    render(<MetricsPanel scope="node" refId="nod_1" family="gpu" />)
    expect(
      screen.queryByText(/gateway's own \/metrics/)
    ).not.toBeInTheDocument()
    expect(screen.getByText("GPU utilization")).toBeInTheDocument()
  })

  it("says why it is empty instead of drawing an idle fleet", () => {
    answer(metrics({ unavailable: "no metrics endpoint on this cluster" }))
    render(<MetricsPanel scope="deployment" refId="dep_1" family="inference" />)
    expect(
      screen.getByText("no metrics endpoint on this cluster")
    ).toBeInTheDocument()
  })

  it("names a workload's breakdown members clusters and reads them by family", () => {
    const breakdown = [
      {
        key: "clu_a",
        label: "us-cluster-1",
        samples: [
          sample("inference.kv_cache_utilization", 62),
          sample("inference.ttft", 0.42),
        ],
      },
      {
        key: "clu_b",
        label: "ap-cluster-1",
        samples: [
          sample("inference.kv_cache_utilization", 34),
          sample("inference.ttft", 0.88),
        ],
      },
    ]
    answer(metrics({ scope: "workload", ref: "wkl_1", breakdown }))
    render(<MetricsPanel scope="workload" refId="wkl_1" family="inference" />)

    expect(screen.getByText("Per cluster")).toBeInTheDocument()
    expect(screen.getByText("us-cluster-1")).toBeInTheDocument()
    // The serving family reads its own two numbers, not a card's heat.
    expect(screen.getByText("34%")).toBeInTheDocument()
    expect(screen.getByText("880 ms")).toBeInTheDocument()
    // 88°C is the GPU column this row would carry under the other family.
    expect(screen.queryByText("88°C")).not.toBeInTheDocument()
  })
})
