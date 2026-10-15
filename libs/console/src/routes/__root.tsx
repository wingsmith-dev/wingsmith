import { ErrorScreen, NotFoundScreen } from "@/components/layout/error-screen"
import { Toaster } from "@/components/ui/sonner"
import { useAuthStore } from "@/modules/auth/store"
import { ReactQueryDevtools } from "@tanstack/react-query-devtools"
import {
  Outlet,
  createRootRoute,
  redirect,
  useRouter,
} from "@tanstack/react-router"
import { TanStackRouterDevtools } from "@tanstack/react-router-devtools"

function RootErrorComponent({ error }: { error: unknown }) {
  const router = useRouter()
  return (
    <ErrorScreen
      error={error}
      reset={() => router.invalidate()}
      className="min-h-svh bg-background"
    />
  )
}

export const Route = createRootRoute({
  beforeLoad: ({ location }) => {
    if (location.pathname !== "/") return
    const { isAuthenticated } = useAuthStore.getState()
    throw redirect({ to: isAuthenticated ? "/overview" : "/login" })
  },
  errorComponent: RootErrorComponent,
  notFoundComponent: () => (
    <NotFoundScreen className="min-h-svh bg-background" />
  ),
  component: RootComponent,
})

function RootComponent() {
  return (
    <>
      <Outlet />
      <Toaster richColors position="bottom-right" />
      {import.meta.env.DEV && (
        <>
          <TanStackRouterDevtools position="bottom-right" />
          <ReactQueryDevtools initialIsOpen={false} />
        </>
      )}
    </>
  )
}
