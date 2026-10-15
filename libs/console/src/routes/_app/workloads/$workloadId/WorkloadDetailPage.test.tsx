import { screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterAll, describe, expect, it, vi } from "vitest"

vi.stubEnv("VITE_API_MODE", "fixtures")
const { renderRoute } = await import("@/test/app")
const { workloads } = await import("@/modules/offline/data")

afterAll(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

const llama = workloads.find((workload) => workload.name === "llama-70b-prod")!

describe("workload detail", () => {
  it(
    "renders the workload and switches between its views",
    { timeout: 15_000 },
    async () => {
      const user = userEvent.setup()
      await renderRoute(`/workloads/${llama.id}`)

      const main = await screen.findByRole("main")
      expect(
        await within(main).findByRole(
          "heading",
          { name: llama.name },
          { timeout: 5000 }
        )
      ).toBeInTheDocument()

      await user.click(within(main).getByRole("tab", { name: /Placement/ }))
      expect(
        await within(main).findByText("Scheduling constraints")
      ).toBeInTheDocument()
      expect(within(main).getByText(llama.priority_class)).toBeInTheDocument()

      await user.click(within(main).getByRole("tab", { name: /Workbench/ }))
      expect(await within(main).findByText("Run history")).toBeInTheDocument()
      expect(within(main).getByText("Runs recorded")).toBeInTheDocument()
    }
  )
})
