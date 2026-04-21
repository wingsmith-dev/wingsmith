import { describe, expect, it } from "vitest"
import {
  LIMIT_SCOPE_MEANING,
  beyondSafeInteger,
  commitsNothing,
  formatLimit,
  formatObjective,
  formatWindow,
  queueTimeoutFor,
  shortfallLabel,
} from "./display"
import type { ServingQuota } from "./types"

function quota(over: Partial<ServingQuota> = {}): ServingQuota {
  return {
    id: "sqt_1",
    endpoint: "end_1",
    endpoint_name: "gpt-oss-chat",
    project: "prj_1",
    committed_rpm: 1_000,
    committed_tpm: 10_000_000,
    max_concurrent_requests: 64,
    max_output_tokens: 4_096,
    burst_rpm: 1_500,
    ramp_up_seconds: 30,
    on_exceed: "reject",
    queue_timeout_ms: null,
    limit_scope: "per_replica",
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

describe("formatLimit", () => {
  it("groups a committed token rate rather than compacting it", () => {
    // "10M" cannot say whether 10,500,000 was what somebody sold.
    expect(formatLimit(10_000_000)).toBe("10,000,000")
    expect(formatLimit(10_500_000)).toBe("10,500,000")
  })

  it("leaves a small rate alone", () => {
    expect(formatLimit(1_000)).toBe("1,000")
    expect(formatLimit(0)).toBe("0")
  })
})

describe("beyondSafeInteger", () => {
  it("passes the rates a commitment is actually written in", () => {
    expect(beyondSafeInteger(10_000_000)).toBe(false)
    expect(beyondSafeInteger(Number.MAX_SAFE_INTEGER)).toBe(false)
  })

  it("flags a BIGINT that a JS number cannot hold exactly", () => {
    expect(beyondSafeInteger(Number.MAX_SAFE_INTEGER + 2)).toBe(true)
  })

  it("does not call an uncommitted limit unreadable", () => {
    expect(beyondSafeInteger(null)).toBe(false)
  })
})

describe("formatObjective", () => {
  it("does not let binary floating point turn three nines into 99.89999", () => {
    expect(formatObjective(0.999)).toBe("99.9%")
    expect(formatObjective(0.9995)).toBe("99.95%")
    expect(formatObjective(1)).toBe("100%")
  })
})

describe("formatWindow", () => {
  it("reads the plane's 60..3600 second window back in its own units", () => {
    expect(formatWindow(600)).toBe("10m")
    expect(formatWindow(3600)).toBe("1h")
    expect(formatWindow(60)).toBe("1m")
    expect(formatWindow(90)).toBe("90s")
  })
})

describe("queueTimeoutFor", () => {
  it("keeps the wait bound while traffic queues", () => {
    expect(queueTimeoutFor("queue", 2_000)).toBe(2_000)
  })

  it("drops a timeout that would mean nothing beside a straight refusal", () => {
    expect(queueTimeoutFor("reject", 2_000)).toBeNull()
  })
})

describe("commitsNothing", () => {
  it("is false while any axis carries a number", () => {
    expect(commitsNothing(quota())).toBe(false)
    expect(
      commitsNothing(
        quota({
          committed_rpm: null,
          committed_tpm: 10_000_000,
          max_concurrent_requests: null,
          max_output_tokens: null,
          burst_rpm: null,
        })
      )
    ).toBe(false)
  })

  it("is true for a row that limits nothing at all", () => {
    // Every axis uncommitted has the same effect as having no row.
    expect(
      commitsNothing(
        quota({
          committed_rpm: null,
          committed_tpm: null,
          max_concurrent_requests: null,
          max_output_tokens: null,
          burst_rpm: null,
        })
      )
    ).toBe(true)
  })
})

describe("shortfallLabel", () => {
  it("names the standard's fields in the words the table uses", () => {
    expect(shortfallLabel("committed_rpm")).toBe("requests/min")
    expect(shortfallLabel("committed_tpm")).toBe("tokens/min")
    expect(shortfallLabel("success_rate_objective")).toBe("success rate")
  })

  it("degrades readably for a field the console has not met yet", () => {
    expect(shortfallLabel("some_new_floor")).toBe("some new floor")
  })
})

describe("LIMIT_SCOPE_MEANING", () => {
  it("states the error direction each mode chooses", () => {
    // The tradeoff has to be legible where the choice is made, not in a doc.
    expect(LIMIT_SCOPE_MEANING.per_replica).toMatch(/times what was sold/)
    expect(LIMIT_SCOPE_MEANING.divided).toMatch(/refuses callers/)
  })
})
