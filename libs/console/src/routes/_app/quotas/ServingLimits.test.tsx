import { render, screen, waitFor } from "@/test/render"
import userEvent from "@testing-library/user-event"
import { beforeAll, describe, expect, it } from "vitest"
import {
  Committed,
  EnforcementBadge,
  OnExceedBadge,
  ScopeBadge,
  ShadowNotice,
  StandardBadge,
} from "./ServingLimits"

// jsdom has no ResizeObserver, and the tooltips here measure their trigger.
beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
})

describe("Committed", () => {
  it("draws an uncommitted limit as its own state and never as zero", () => {
    // A blank committed_rpm rendered as "0" would read as "refuse everything",
    // which is the opposite of what an uncommitted axis means.
    render(<Committed value={null} />)
    expect(screen.getByText("uncommitted")).toBeInTheDocument()
    expect(screen.queryByText("0")).not.toBeInTheDocument()
  })

  it("still renders a real zero as a number", () => {
    render(<Committed value={0} />)
    expect(screen.getByText("0")).toBeInTheDocument()
    expect(screen.queryByText("uncommitted")).not.toBeInTheDocument()
  })

  it("groups a committed token rate", () => {
    render(<Committed value={10_000_000} />)
    expect(screen.getByText("10,000,000")).toBeInTheDocument()
  })

  it("refuses to print a limit past what a JS number holds exactly", () => {
    render(<Committed value={Number.MAX_SAFE_INTEGER + 2} />)
    expect(screen.getByText("too large to read")).toBeInTheDocument()
  })

  it("explains that uncommitted is not a limit of zero", async () => {
    render(<Committed value={null} />)
    await userEvent.hover(screen.getByText("uncommitted"))
    await waitFor(() =>
      expect(screen.getAllByText(/Not a limit of zero/).length).toBeGreaterThan(
        0
      )
    )
  })
})

describe("EnforcementBadge", () => {
  it("names shadow mode outright", () => {
    render(<EnforcementBadge enforced={false} />)
    expect(screen.getByText("shadow mode")).toBeInTheDocument()
  })

  it("says enforced when the gateway will actually refuse", () => {
    render(<EnforcementBadge enforced />)
    expect(screen.getByText("enforced")).toBeInTheDocument()
    expect(screen.queryByText("shadow mode")).not.toBeInTheDocument()
  })

  it("spells out that nothing is refused in shadow mode", async () => {
    render(<EnforcementBadge enforced={false} />)
    await userEvent.hover(screen.getByText("shadow mode"))
    await waitFor(() =>
      expect(
        screen.getAllByText(/never refuses a request/).length
      ).toBeGreaterThan(0)
    )
  })
})

describe("ShadowNotice", () => {
  it("counts the commitments nothing upholds", () => {
    render(<ShadowNotice shadowed={3} total={7} />)
    expect(screen.getByText(/3 of 7/)).toBeInTheDocument()
  })

  it("stays out of the way when every commitment is enforced", () => {
    const { container } = render(<ShadowNotice shadowed={0} total={7} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe("ScopeBadge", () => {
  it("labels the two scopes apart", () => {
    const { unmount } = render(<ScopeBadge scope="per_replica" />)
    expect(screen.getByText("per replica")).toBeInTheDocument()
    unmount()
    render(<ScopeBadge scope="divided" />)
    expect(screen.getByText("divided")).toBeInTheDocument()
  })

  it("explains the error direction the mode chooses", async () => {
    render(<ScopeBadge scope="divided" />)
    await userEvent.hover(screen.getByText("divided"))
    await waitFor(() =>
      expect(screen.getAllByText(/refuses callers/).length).toBeGreaterThan(0)
    )
  })
})

describe("OnExceedBadge", () => {
  it("carries the wait bound while traffic queues", () => {
    render(<OnExceedBadge onExceed="queue" queueTimeoutMs={2000} />)
    expect(screen.getByText(/2000ms/)).toBeInTheDocument()
  })

  it("says only what happens when nothing queues", () => {
    render(<OnExceedBadge onExceed="reject" queueTimeoutMs={null} />)
    expect(screen.getByText("reject")).toBeInTheDocument()
    expect(screen.queryByText(/ms/)).not.toBeInTheDocument()
  })
})

describe("StandardBadge", () => {
  it("names the fields that fall short, not only failing", () => {
    render(
      <StandardBadge
        standard={{
          meets: false,
          shortfalls: ["committed_tpm", "success_rate_objective"],
        }}
      />
    )
    expect(
      screen.getByText("short: tokens/min, success rate")
    ).toBeInTheDocument()
  })

  it("says a commitment clears the standard's floors", () => {
    render(<StandardBadge standard={{ meets: true, shortfalls: [] }} />)
    expect(screen.getByText("meets standard")).toBeInTheDocument()
  })
})
