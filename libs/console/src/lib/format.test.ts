import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  formatAbsolute,
  formatError,
  formatRelative,
  shortenId,
} from "./format"

describe("formatRelative", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date("2026-08-23T12:00:00Z"))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("says never instead of drawing an epoch for an absent timestamp", () => {
    // A row that has never reported is not a row that reported in 1970.
    expect(formatRelative(null)).toBe("never")
    expect(formatRelative("")).toBe("never")
  })

  it("picks the unit that makes the number readable", () => {
    const cases: [string, string][] = [
      ["2026-08-23T11:59:30Z", "30 seconds ago"],
      ["2026-08-23T11:45:00Z", "15 minutes ago"],
      ["2026-08-23T09:00:00Z", "3 hours ago"],
      ["2026-08-21T12:00:00Z", "2 days ago"],
      ["2026-08-09T12:00:00Z", "2 weeks ago"],
      ["2026-05-23T12:00:00Z", "3 months ago"],
      ["2024-08-23T12:00:00Z", "2 years ago"],
    ]
    for (const [at, want] of cases) {
      expect(formatRelative(at)).toBe(want)
    }
  })

  it("reads the future as the future", () => {
    // Deadlines and scheduled operations are both drawn with this.
    expect(formatRelative("2026-08-23T12:30:00Z")).toBe("in 30 minutes")
  })

  it("uses the natural word where there is one", () => {
    expect(formatRelative("2026-08-22T12:00:00Z")).toBe("yesterday")
    expect(formatRelative("2026-08-24T12:00:00Z")).toBe("tomorrow")
  })
})

describe("formatAbsolute", () => {
  it("draws an em dash instead of an epoch for an absent timestamp", () => {
    expect(formatAbsolute(null)).toBe("—")
    expect(formatAbsolute("")).toBe("—")
  })

  it("renders a real timestamp as something with the date and the time in it", () => {
    const got = formatAbsolute("2026-08-23T12:00:00Z")
    expect(got).not.toBe("—")
    expect(got).toMatch(/2026/)
  })
})

describe("formatError", () => {
  it("prefers the error's own message", () => {
    expect(formatError(new Error("the plane is unreachable"))).toBe(
      "the plane is unreachable"
    )
  })

  it("falls back for anything that is not an Error", () => {
    // A rejected fetch can throw a string, a Response, or undefined.
    expect(formatError("boom")).toBe("Failed to load")
    expect(formatError(undefined)).toBe("Failed to load")
    expect(formatError({ status: 500 })).toBe("Failed to load")
    expect(formatError(null, "Could not load devices")).toBe(
      "Could not load devices"
    )
  })
})

describe("shortenId", () => {
  it("keeps a prefix so the kind is still readable", () => {
    expect(shortenId("dev_01j9x2aaaaaaaaaaaaaaaaaaaa")).toBe("dev_01j9x2")
    expect(shortenId("clu_01j9x2aaaaaaaaaaaaaaaaaaaa")).toBe("clu_01j9x2")
  })

  it("keeps a vendor tag so a GPU serial still reads as one", () => {
    expect(shortenId("GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")).toBe(
      "GPU-aaaaaa"
    )
    expect(shortenId("MIG-12345678-1234")).toBe("MIG-123456")
  })

  it("honors the requested width", () => {
    expect(shortenId("dev_01j9x2aaaaaaaa", 4)).toBe("dev_01j9")
    expect(shortenId("dev_01j9x2aaaaaaaa", 10)).toBe("dev_01j9x2aaaa")
  })

  it("leaves something already short alone instead of adding an ellipsis", () => {
    // Two rows only need to be tellable apart; a value that is already short is fine.
    expect(shortenId("iad-1")).toBe("iad-1")
    expect(shortenId("n01")).toBe("n01")
    expect(shortenId("")).toBe("")
  })

  it("cuts an untagged identifier to a fixed width", () => {
    expect(shortenId("0123456789abcdef")).toBe("0123456789")
    expect(shortenId("0123456789abcdef", 4)).toBe("01234567")
  })

  it("keeps distinct ids distinct at the width a table uses", () => {
    // The point: if two rows collapsed to the same string the table would lie.
    const ids = [
      "dev_01j9x2aaaaaaaa",
      "dev_01j9x3aaaaaaaa",
      "dev_01j9x4aaaaaaaa",
    ]
    expect(new Set(ids.map((id) => shortenId(id))).size).toBe(ids.length)
  })
})
