import { describe, expect, it } from "vitest"

/**
 * The console must not render operational numbers the plane did not produce.
 *
 * `TODO-API` comments and `MOCK_*` constants were how invented figures got onto
 * real pages before; this fails the suite if either comes back. Fixture data
 * belongs in `modules/offline/` (the offline demo layer) or in tests.
 *
 * `modules/benchmarks/mock.ts` is the one allowed exception: it feeds workbench
 * views that are reachable only behind an explicit Mock switch or for harness
 * kinds that do not exist yet, and every consumer renders `MockNotice` above it.
 */
const sources = import.meta.glob<string>(
  [
    "./**/*.{ts,tsx}",
    "!./**/*.test.{ts,tsx}",
    "!./test/**",
    "!./modules/offline/**",
    "!./modules/benchmarks/mock.ts",
    "!./routeTree.gen.ts",
  ],
  { query: "?raw", import: "default", eager: true }
)

const FORBIDDEN: [string, RegExp][] = [
  ["TODO-API marker", /TODO-API/],
  ["MOCK_ identifier", /\bMOCK_[A-Z0-9_]*/],
]

describe("no invented data outside fixtures", () => {
  it("scans the source tree", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })

  it("has no TODO-API markers or MOCK_ identifiers", () => {
    const hits: string[] = []
    for (const [file, text] of Object.entries(sources)) {
      text.split("\n").forEach((line, i) => {
        for (const [what, pattern] of FORBIDDEN) {
          if (pattern.test(line)) hits.push(`${file}:${i + 1} ${what}`)
        }
      })
    }
    expect(hits).toEqual([])
  })
})
