import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render as rtlRender } from "@testing-library/react"
import type { ReactElement, ReactNode } from "react"
import { TooltipProvider } from "@/components/ui/tooltip"

/**
 * Renders a component under the providers the app supplies at its root.
 *
 * Components reach for these through context instead of props (a tooltip
 * without its provider throws), so a test that rendered them bare would be
 * testing something the app never mounts.
 */
export function render(
  ui: ReactElement,
  options: { client?: QueryClient } = {}
) {
  const client =
    options.client ??
    new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })

  function Providers({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <TooltipProvider>{children}</TooltipProvider>
      </QueryClientProvider>
    )
  }

  return { ...rtlRender(ui, { wrapper: Providers }), client }
}

/* eslint-disable-next-line react-refresh/only-export-components -- a test helper, not a hot-reloaded module */
export * from "@testing-library/react"
