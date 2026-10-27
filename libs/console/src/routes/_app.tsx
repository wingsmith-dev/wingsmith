import { ConsoleSidebar } from "@/components/layout/sidebar-console"
import { ErrorScreen } from "@/components/layout/error-screen"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import { useAuthStore } from "@/modules/auth/store"
import {
  createFileRoute,
  Outlet,
  redirect,
  useRouter,
} from "@tanstack/react-router"

function AppErrorComponent({ error }: { error: unknown }) {
  const router = useRouter()
  return (
    <ErrorScreen
      error={error}
      reset={() => router.invalidate()}
      className="min-h-svh bg-background"
    />
  )
}

/**
 * The operator console. A tenant session reaches nothing under here, the plane
 * refuses it on every route this shell reads, so it is sent to its own view
 * instead, which is what makes the boundary a boundary instead of a screen full
 * of failed panels.
 */
export const Route = createFileRoute("/_app")({
  errorComponent: AppErrorComponent,
  beforeLoad: () => {
    const session = useAuthStore.getState()
    if (!session.isAuthenticated) {
      throw redirect({ to: "/login" })
    }
    if (session.kind === "tenant") {
      throw redirect({ to: "/serving" })
    }
  },
  component: AppLayout,
})

function AppLayout() {
  return (
    <SidebarProvider>
      <ConsoleSidebar />
      <SidebarInset className="min-w-0">
        <Outlet />
      </SidebarInset>
    </SidebarProvider>
  )
}
