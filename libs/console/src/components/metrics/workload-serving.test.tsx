import { render, screen } from "@/test/render"
import { metricDefinition } from "@/modules/metrics/catalogue"
import type { Metrics } from "@/modules/metrics/types"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { WorkloadServingSection } from "./workload-serving"

vi.mock("@/modules/metrics/queries", () => ({ useMetrics: vi.fn() }))

const { useMetrics } = await import("@/modules/metrics/queries")
const mocked = vi.mocked(useMetrics)

const PER_STREAM = "inference.output_tokens_per_stream"

/** Two clusters serving the same workload at deliberately different rates. */
const BY_REF: Record<string, number> = { dep_ap: 28.5, dep_us: 33.5 }

function deployment(id: string, cluster: string) {
  return { id, name: `llama-${cluster}`, cluster_name: cluster }
}

beforeEach(() => {
  mocked.mockReset()
  mocked.mockImplementation((_scope, ref) => {
    const data: Metrics = {
      scope: "deployment",
      ref: ref ?? "",
      cluster: null,
      source: null,
      window: "5m",
      samples: [
        {
          metric: PER_STREAM,
          title: "Output tokens/sec, one stream",
          unit: metricDefinition(PER_STREAM)!.unit,
          value: BY_REF[ref ?? ""] ?? null,
        },
      ],
      breakdown: [],
      series: [],
      unavailable: null,
    }
    return { data, isLoading: false, error: null } as unknown as ReturnType<
      typeof useMetrics
    >
  })
})

describe("WorkloadServingSection", () => {
  it("draws one panel per cluster and never blends them into a workload number", () => {
    render(
      <WorkloadServingSection
        deployments={[
          deployment("dep_ap", "ap-cluster-1"),
          deployment("dep_us", "us-cluster-1"),
        ]}
      />
    )

    expect(screen.getByText("ap-cluster-1")).toBeInTheDocument()
    expect(screen.getByText("us-cluster-1")).toBeInTheDocument()
    expect(screen.getByText("28.5/s")).toBeInTheDocument()
    expect(screen.getByText("33.5/s")).toBeInTheDocument()
    // 62.0/s would be the sum of two per-stream rates, which is not a number.
    expect(screen.queryByText("62.0/s")).not.toBeInTheDocument()
    expect(screen.getByText(/not added up or averaged across/i)).toBeVisible()
  })

  it("says nothing about averaging when there is only one cluster", () => {
    render(
      <WorkloadServingSection
        deployments={[deployment("dep_us", "us-cluster-1")]}
      />
    )
    expect(
      screen.queryByText(/not added up or averaged across/i)
    ).not.toBeInTheDocument()
  })

  it("renders nothing for a workload that has been placed nowhere", () => {
    const { container } = render(<WorkloadServingSection deployments={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
