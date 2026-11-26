import { describe, expect, it } from "vitest"
import { render, screen } from "@/test/render"
import { DemoNotice } from "./DemoNotice"

describe("DemoNotice", () => {
  it("shows the password when one is set", () => {
    render(<DemoNotice password="open-sesame" />)
    expect(screen.getByRole("note").textContent).toContain(
      "Sign in with password: open-sesame"
    )
  })

  it("renders nothing when unset", () => {
    render(<DemoNotice password="" />)
    expect(screen.queryByRole("note")).toBeNull()
  })
})
