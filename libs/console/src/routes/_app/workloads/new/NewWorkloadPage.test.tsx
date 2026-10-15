import { screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterAll, describe, expect, it, vi } from "vitest"

vi.stubEnv("VITE_API_MODE", "fixtures")
const { renderRoute } = await import("@/test/app")

afterAll(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe("new workload", () => {
  it(
    "opens on the artifact step and moves on once one is picked",
    { timeout: 15_000 },
    async () => {
      const user = userEvent.setup()
      await renderRoute("/workloads/new")

      const main = await screen.findByRole("main")
      expect(
        await within(main).findByRole("heading", { name: "New workload" })
      ).toBeInTheDocument()
      expect(
        within(main).getByPlaceholderText("Search artifacts by name or digest")
      ).toBeInTheDocument()

      await user.click(
        await within(main).findByRole(
          "button",
          { name: /llama-3\.3-70b-instruct/ },
          { timeout: 5000 }
        )
      )

      // The picked artifact becomes the step's answer, and the search closes.
      expect(
        await within(main).findByText("llama-3.3-70b-instruct", {
          selector: "span",
        })
      ).toBeInTheDocument()
      expect(
        within(main).queryByPlaceholderText(
          "Search artifacts by name or digest"
        )
      ).toBeNull()
    }
  )
})
