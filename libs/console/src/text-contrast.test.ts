import { describe, expect, it } from "vitest"

/**
 * DESIGN.md commits the console to WCAG 2.1 AA text contrast. The theme tokens
 * are chosen to hold 4.5:1 on every surface; what breaks that is fading text
 * with an opacity modifier, or reaching for a raw palette colour that only
 * reads on one theme. This fails the suite if either comes back.
 */
const sources = import.meta.glob<string>(
  ["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}", "!./routeTree.gen.ts"],
  { query: "?raw", import: "default", eager: true }
)

const FORBIDDEN: [string, RegExp][] = [
  [
    "faded text colour (use text-muted-foreground)",
    /(?<![\w-])(?:[a-z-]+:)*text-(?:foreground|muted-foreground|primary|destructive|warning|success)\/\d+/,
  ],
  [
    "raw amber/emerald text (use text-warning or text-success)",
    /(?<![\w-])(?:[a-z-]+:)*text-(?:amber|emerald|yellow|green)-\d{3}/,
  ],
]

describe("text contrast", () => {
  it("scans the source tree", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })

  it("never fades text or uses a single-theme warning colour", () => {
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
