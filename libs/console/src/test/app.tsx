import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  RouterProvider,
  createMemoryHistory,
  createRouter,
} from "@tanstack/react-router"
import { render } from "@testing-library/react"
import { vi } from "vitest"
import { ThemeProvider } from "@/components/theme-provider"
import { TooltipProvider } from "@/components/ui/tooltip"
import { useAuthStore } from "@/modules/auth/store"
import { routeTree } from "@/routeTree.gen"

/**
 * Renders the whole console at `path`, signed in as the admin, against the offline
 * plane. The test file must set `VITE_API_MODE=fixtures` before importing this
 * module (the transport is chosen when `lib/api` loads), which is why page tests
 * import it dynamically after `vi.stubEnv`.
 */
export async function renderRoute(path: string) {
  // jsdom has neither; the sidebar reads one and the charts the other.
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }) as MediaQueryList
  )
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  )

  await useAuthStore.getState().signIn("fixtures")

  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
  })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  const view = render(
    <ThemeProvider storageKey="hangar.theme.test">
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  )
  return { ...view, router }
}
