import { render, screen } from "@/test/render"
import { describe, expect, it, vi } from "vitest"
import type { AlertThreshold, MetricRule } from "@/modules/alerts/types"
import { ThresholdResolution } from "./threshold-resolution"

const RULE = "deployment.ttft_p90_high"

function threshold(over: Partial<AlertThreshold> = {}): AlertThreshold {
  return {
    id: "alt_1",
    rule: RULE,
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

const RULES: MetricRule[] = [
  {
    name: RULE,
    metric: "inference.ttft_p90",
    unit: "seconds",
    comparison: "above",
    subject_type: "deployment",
    severity: "warning",
    measures: "time to first token (p90)",
    suggested_value: 1.5,
    lookback: "5m",
    for_seconds: 900,
  },
]

describe("ThresholdResolution", () => {
  it("says when the number applying to a subject is the fleet default", () => {
    render(
      <ThresholdResolution
        rule={RULE}
        subjectUid="dep_1"
        thresholds={[threshold()]}
        rules={RULES}
      />
    )
    expect(screen.getByText("inherited · fleet default")).toBeInTheDocument()
    expect(
      screen.getByText(/time to first token \(p90\) above 1.50 s/)
    ).toBeInTheDocument()
  })

  it("says when the subject has taken itself out of the fleet default", () => {
    render(
      <ThresholdResolution
        rule={RULE}
        subjectUid="dep_1"
        thresholds={[
          threshold({ id: "alt_fleet" }),
          threshold({
            id: "alt_scoped",
            subject: { type: "deployment", id: "dep_1" },
            value: 4,
          }),
        ]}
        rules={RULES}
      />
    )
    expect(screen.getByText("overridden for this subject")).toBeInTheDocument()
    expect(
      screen.getByText(/time to first token \(p90\) above 4.00 s/)
    ).toBeInTheDocument()
    expect(screen.getByText("Shadowed")).toBeInTheDocument()
  })

  // The 3am mistake this screen exists to prevent: raising the number on a row
  // that is switched off while the fleet default keeps firing.
  it("warns that a disabled override is not the number that fired", () => {
    render(
      <ThresholdResolution
        rule={RULE}
        subjectUid="dep_1"
        thresholds={[
          threshold({ id: "alt_fleet" }),
          threshold({
            id: "alt_scoped",
            subject: { type: "deployment", id: "dep_1" },
            value: 4,
            enabled: false,
          }),
        ]}
        rules={RULES}
      />
    )
    expect(screen.getByText("inherited · fleet default")).toBeInTheDocument()
    expect(screen.getByText("Declared but switched off")).toBeInTheDocument()
    expect(screen.getByText("disabled")).toBeInTheDocument()
  })

  it("marks a window that came from the rule, not from the row", () => {
    render(
      <ThresholdResolution
        rule={RULE}
        subjectUid="dep_1"
        thresholds={[threshold({ lookback: "30m" })]}
        rules={RULES}
      />
    )
    expect(screen.getByText("30m")).toBeInTheDocument()
    // The hold was not declared, so it is shown as the rule's own.
    expect(screen.getByText("15m")).toBeInTheDocument()
    expect(screen.getAllByText("· from the rule").length).toBe(2)
  })

  it("offers to override a fleet default, not edit it in place", async () => {
    const onDeclare = vi.fn()
    render(
      <ThresholdResolution
        rule={RULE}
        subjectUid="dep_1"
        thresholds={[threshold()]}
        rules={RULES}
        onDeclare={onDeclare}
      />
    )
    screen.getByRole("button", { name: "Override for this subject" }).click()
    expect(onDeclare).toHaveBeenCalledOnce()
  })

  it("reports a rule nobody has declared a number for", () => {
    render(
      <ThresholdResolution
        rule={RULE}
        subjectUid="dep_1"
        thresholds={[]}
        rules={RULES}
      />
    )
    expect(screen.getByText(/evaluates it for nobody/)).toBeInTheDocument()
    expect(screen.getByText(/1.50 s/)).toBeInTheDocument()
  })

  // Conditions like `cluster.unreachable` are compiled in and have no declared
  // number at all; claiming otherwise would send someone looking for a row.
  it("does not pretend a compiled condition has a threshold behind it", () => {
    render(
      <ThresholdResolution
        rule="cluster.unreachable"
        subjectUid="clu_1"
        thresholds={[]}
        rules={RULES}
      />
    )
    expect(
      screen.getByText(/compiled condition, not a metric rule/)
    ).toBeInTheDocument()
  })
})
