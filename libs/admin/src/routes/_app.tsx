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
  return <ErrorScreen error={error} reset={() => router.invalidate()} />
}

export const Route = createFileRoute("/_app")({
  errorComponent: AppErrorComponent,
  beforeLoad: () => {
    if (!useAuthStore.getState().isAuthenticated) {
      throw redirect({ to: "/login" })
    }
  },
  component: AppLayout,
})

function AppLayout() {
  return (
    <SidebarProvider>
      <ConsoleSidebar />
      <SidebarInset>
        <Outlet />
      </SidebarInset>
    </SidebarProvider>
  )
}
