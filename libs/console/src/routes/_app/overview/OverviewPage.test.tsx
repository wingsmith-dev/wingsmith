import { screen, within } from "@testing-library/react"
import { afterAll, describe, expect, it, vi } from "vitest"

vi.stubEnv("VITE_API_MODE", "fixtures")
const { renderRoute } = await import("@/test/app")
const { workloads: fixtureWorkloads } = await import("@/modules/offline/data")

afterAll(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe("overview", () => {
  it(
    "renders the fleet against the offline plane",
    { timeout: 15_000 },
    async () => {
      await renderRoute("/overview")

      expect(
        await screen.findByRole("heading", { name: "Overview" })
      ).toBeInTheDocument()
      // Each card resolves once its drained query lands.
      const main = await screen.findByRole("main")
      const workloads = await within(main).findByText("Workloads", {
        selector: "div",
      })
      const card = workloads.closest("a")!
      expect(card).toHaveAttribute("href", "/workloads")
      await vi.waitFor(
        () =>
          expect(
            within(card).getByText(String(fixtureWorkloads.length))
          ).toBeInTheDocument(),
        { timeout: 5000 }
      )
      expect(
        await screen.findByText("Capacity pressure", undefined, {
          timeout: 5000,
        })
      ).toBeInTheDocument()
      expect(screen.queryByText(/counts on this page are partial/)).toBeNull()
    }
  )
})
