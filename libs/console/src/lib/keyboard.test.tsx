import { describe, expect, it, vi } from "vitest"
import userEvent from "@testing-library/user-event"

import { render, screen } from "@/test/render"
import { activationProps } from "./keyboard"

function Row({
  onActivate,
  onInner,
}: {
  onActivate?: () => void
  onInner?: () => void
}) {
  return (
    <div data-testid="row" {...activationProps(onActivate)}>
      <button onClick={onInner}>inner</button>
    </div>
  )
}

describe("activationProps", () => {
  it("adds nothing when there is nothing to activate", () => {
    render(<Row />)
    expect(screen.getByTestId("row")).not.toHaveAttribute("tabindex")
  })

  it("activates on click, Enter and Space once focused", async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    render(<Row onActivate={onActivate} />)
    const row = screen.getByTestId("row")
    expect(row).toHaveAttribute("tabindex", "0")

    await user.click(row)
    row.focus()
    await user.keyboard("{Enter}")
    await user.keyboard(" ")
    expect(onActivate).toHaveBeenCalledTimes(3)
  })

  it("leaves keys on an inner control to that control", async () => {
    const user = userEvent.setup()
    const onActivate = vi.fn()
    const onInner = vi.fn()
    render(<Row onActivate={onActivate} onInner={onInner} />)
    screen.getByText("inner").focus()
    await user.keyboard("{Enter}")
    expect(onInner).toHaveBeenCalledTimes(1)
    expect(onActivate).not.toHaveBeenCalled()
  })
})
