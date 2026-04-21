import { render, screen } from "@/test/render"
import { beforeAll, describe, expect, it, vi } from "vitest"
import type {
  ResolvedServingQuotaTable,
  ServingQuota,
} from "@/modules/binding/types"
import { ServingQuotaSheet } from "./ServingQuotaSheet"

const resolved = vi.hoisted(() => vi.fn())

// The sheet reaches for exactly one hook; mocking the module whole keeps the
// transport and the fixture corpus out of a rendering test.
vi.mock("@/modules/binding/queries", () => ({
  useResolvedServingQuotas: resolved,
}))

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
})

function quota(over: Partial<ServingQuota> = {}): ServingQuota {
  return {
    id: "sqt_1",
    endpoint: "end_1",
    endpoint_name: "gpt-oss-chat",
    project: "prj_1",
    committed_rpm: 1_000,
    committed_tpm: 10_000_000,
    max_concurrent_requests: 64,
    max_output_tokens: null,
    burst_rpm: null,
    ramp_up_seconds: 30,
    on_exceed: "reject",
    queue_timeout_ms: null,
    limit_scope: "divided",
    success_rate_objective: 0.999,
    availability_objective: "availability-2026.1",
    objective_window_seconds: 600,
    enforced: true,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: null,
    revision: "1",
    standard: { meets: true, shortfalls: [] },
    ...over,
  }
}

function table(over: Partial<ResolvedServingQuotaTable> = {}) {
  const limits = {
    committed_rpm: 250,
    burst_rpm: null,
    ramp_up_seconds: 30,
    committed_tpm: 2_500_000,
    max_concurrent_requests: 16,
    max_output_tokens: null,
    on_exceed: "reject" as const,
    queue_timeout_ms: null,
    limit_scope: "divided" as const,
    enforced: true,
  }
  return {
    revision: "r1",
    replicas: 4,
    replica_index: 0,
    quotas: [
      {
        endpoint: "end_1",
        endpoint_name: "gpt-oss-chat",
        project: "prj_1",
        workloads: ["wkl_1"],
        replicas: 4,
        replica_index: 0,
        declared: {
          ...limits,
          committed_rpm: 1_000,
          committed_tpm: 10_000_000,
        },
        effective: limits,
        starved_replicas: 0,
        success_rate_objective: 0.999,
        availability_objective: "availability-2026.1",
        objective_window_seconds: 600,
        standard: { meets: true, shortfalls: [] },
        revision: "r1",
      },
    ],
    ...over,
  } satisfies ResolvedServingQuotaTable
}

describe("ServingQuotaSheet", () => {
  it("shows the plane's share of a divided limit rather than dividing here", () => {
    resolved.mockReturnValue({ data: table(), error: null })
    render(<ServingQuotaSheet quota={quota()} onOpenChange={() => {}} />)

    expect(screen.getByText("10,000,000")).toBeInTheDocument()
    expect(screen.getByText("2,500,000")).toBeInTheDocument()
    expect(screen.getByText("250")).toBeInTheDocument()
  })

  it("keeps an uncommitted axis uncommitted at every fleet size", () => {
    resolved.mockReturnValue({ data: table(), error: null })
    render(<ServingQuotaSheet quota={quota()} onOpenChange={() => {}} />)

    // A divided null would turn "not limited" into a number, and at a fleet
    // larger than the limit that number would be zero.
    expect(screen.getAllByText("uncommitted").length).toBeGreaterThan(0)
  })

  it("says outright when replicas would hold a share of zero", () => {
    const starved = table()
    starved.quotas[0].starved_replicas = 3
    resolved.mockReturnValue({ data: starved, error: null })
    render(<ServingQuotaSheet quota={quota()} onOpenChange={() => {}} />)

    expect(screen.getByText(/3 of 4 replicas/)).toBeInTheDocument()
  })

  it("warns that a shadow commitment refuses nothing", () => {
    resolved.mockReturnValue({ data: table(), error: null })
    render(
      <ServingQuotaSheet
        quota={quota({ enforced: false })}
        onOpenChange={() => {}}
      />
    )

    expect(screen.getByText("shadow mode")).toBeInTheDocument()
    expect(screen.getByText(/none of it is refused/)).toBeInTheDocument()
  })

  it("says a per-replica fleet admits a multiple of what was sold", () => {
    resolved.mockReturnValue({ data: table(), error: null })
    render(
      <ServingQuotaSheet
        quota={quota({ limit_scope: "per_replica" })}
        onOpenChange={() => {}}
      />
    )

    expect(screen.getAllByText(/admits up to/).length).toBeGreaterThan(0)
  })

  it("surfaces the plane's error instead of guessing at the shares", () => {
    resolved.mockReturnValue({
      data: undefined,
      error: new Error("replicas must be at least 1"),
    })
    render(<ServingQuotaSheet quota={quota()} onOpenChange={() => {}} />)

    expect(screen.getByText("replicas must be at least 1")).toBeInTheDocument()
  })
})
