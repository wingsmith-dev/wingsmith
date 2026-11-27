import { fireEvent, render, screen } from "@/test/render"
import { describe, expect, it, vi } from "vitest"
import { ManagedBy } from "./managed-by"

describe("ManagedBy", () => {
  it("renders nothing for an object no manifest manages", () => {
    const { container } = render(
      <ManagedBy kind="workload" name="w" manager={null} onTakeOver={vi.fn()} />
    )
    expect(container).toBeEmptyDOMElement()
  })

  it("names the manager and takes over only after the confirm step", () => {
    const onTakeOver = vi.fn()
    render(
      <ManagedBy
        kind="workload"
        name="gpt-oss-prod"
        manager="argo"
        principal="ci-bot"
        onTakeOver={onTakeOver}
      />
    )
    expect(screen.getByText("Managed by argo")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Take over" }))
    expect(onTakeOver).not.toHaveBeenCalled()
    expect(screen.getByText("Take over this workload?")).toBeInTheDocument()
    const buttons = screen.getAllByRole("button", { name: "Take over" })
    fireEvent.click(buttons[buttons.length - 1])
    expect(onTakeOver).toHaveBeenCalledOnce()
  })
})
