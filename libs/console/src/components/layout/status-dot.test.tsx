import { render, screen } from "@/test/render"
import { describe, expect, it } from "vitest"
import { StatusDot, StatusLabel, type StatusTone } from "./status-dot"

const TONES: StatusTone[] = ["ok", "warn", "danger", "info", "neutral"]

describe("StatusDot", () => {
  it("draws a distinct color for every tone", () => {
    // Two tones that rendered alike would make a warning indistinguishable from a failure.
    const classes = TONES.map((tone) => {
      const { container, unmount } = render(<StatusDot tone={tone} />)
      const html = container.innerHTML
      unmount()
      return html
    })
    expect(new Set(classes).size).toBe(TONES.length)
  })

  it("animates only when asked to", () => {
    const { container: still } = render(<StatusDot tone="ok" />)
    const { container: pulsing } = render(<StatusDot tone="ok" pulse />)
    expect(still.innerHTML).not.toContain("animate-ping")
    expect(pulsing.innerHTML).toContain("animate-ping")
  })

  it("accepts extra classes without dropping its own", () => {
    const { container } = render(<StatusDot tone="ok" className="mr-2" />)
    expect(container.innerHTML).toContain("mr-2")
    expect(container.innerHTML).toContain("bg-emerald-500")
  })
})

describe("StatusLabel", () => {
  it("puts the dot beside its text rather than replacing it", () => {
    // A color alone is not readable to everyone; the word is the accessible half.
    const { container } = render(
      <StatusLabel tone="danger">unreachable</StatusLabel>
    )
    expect(screen.getByText("unreachable")).toBeInTheDocument()
    expect(container.innerHTML).toContain("bg-destructive")
  })
})
