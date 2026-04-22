import { TooltipProvider } from "@/components/ui/tooltip"
import { QueryClientProvider } from "@tanstack/react-query"
import { RouterProvider, createRouter } from "@tanstack/react-router"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import { ThemeProvider } from "@/components/theme-provider"
import { queryClient } from "@/lib/query-client"
import "./index.css"
import { installUnauthorizedHandler } from "@/modules/auth/session"
import { applyStoredAccent } from "@/modules/settings/store"
import { routeTree } from "./routeTree.gen"

applyStoredAccent()

export const router = createRouter({ routeTree })

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
}

installUnauthorizedHandler(() => {
  void router.navigate({ to: "/login", replace: true })
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider storageKey="hangar.theme">
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>
)
