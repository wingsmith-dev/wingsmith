import { render, screen } from "@/test/render"
import { describe, expect, it, vi } from "vitest"
import type { Alert, AlertThreshold, MetricRule } from "@/modules/alerts/types"
import { AlertDetail } from "./alert-detail"

const RULE = "deployment.ttft_p90_high"

const ALERT: Alert = {
  id: "alr_1",
  rule: RULE,
  source: "condition",
  severity: "warning",
  state: "firing",
  title: "deployment gpt-20b-iad is slow to first token",
  cluster_id: "clu_1",
  subject: { type: "deployment", id: "dep_1" },
  details: { observed: 3.2 },
  occurrences: 4,
  first_seen_at: "2026-08-25T10:00:00Z",
  last_seen_at: "2026-08-25T10:20:00Z",
  fired_at: "2026-08-25T10:15:00Z",
  resolved_at: null,
  acknowledged_at: null,
  acknowledged_by: null,
  acknowledged_note: null,
  notified_at: null,
  notify_error: null,
}

const THRESHOLD: AlertThreshold = {
  id: "alt_fleet",
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
  reason: "the number the launch was signed off against",
  created_at: "2026-08-20T10:00:00Z",
  updated_at: null,
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

function open(overrides: Partial<Alert> = {}) {
  return render(
    <AlertDetail
      alert={{ ...ALERT, ...overrides }}
      thresholds={[THRESHOLD]}
      rules={RULES}
      open
      onOpenChange={vi.fn()}
      onDeclare={vi.fn()}
      onEdit={vi.fn()}
    />
  )
}

describe("AlertDetail", () => {
  it("shows the alert beside the threshold that produced it", () => {
    open()
    expect(screen.getByText(ALERT.title)).toBeInTheDocument()
    expect(screen.getByText("inherited · fleet default")).toBeInTheDocument()
    expect(
      screen.getByText("the number the launch was signed off against")
    ).toBeInTheDocument()
  })

  // Observed state gets no edit affordance. The only input on the sheet is the
  // acknowledgment note, which is the one thing about an alert a machine
  // cannot know: every derived field is text.
  it("offers no way to edit anything the reactor derived", () => {
    open()
    // The sheet renders through a portal, so the assertion is against the
    // document instead of the render container.
    const inputs = document.body.querySelectorAll("input")
    expect(inputs.length).toBe(1)
    expect(screen.getByLabelText("Acknowledgment note")).toBe(inputs[0])
    expect(document.body.querySelectorAll("textarea").length).toBe(0)
  })

  it("carries the two lifecycle verbs while the alert is open", () => {
    open()
    expect(
      screen.getByRole("button", { name: "Acknowledge" })
    ).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Resolve" })).toBeInTheDocument()
  })

  it("drops them once the alert is resolved", () => {
    open({ state: "resolved", resolved_at: "2026-08-25T11:00:00Z" })
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Resolve" })).toBeNull()
  })

  it("says a pending alert has not fired instead of showing a blank", () => {
    open({ state: "pending", fired_at: null })
    expect(screen.getByText("not yet: still pending")).toBeInTheDocument()
  })
})
