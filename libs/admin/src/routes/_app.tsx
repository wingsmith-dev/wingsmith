import { ConsoleSidebar } from "@/components/layout/sidebar-console"
import { ErrorScreen } from "@/components/layout/error-screen"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import { useReadOnly } from "@/modules/settings/store"
import { Link } from "@tanstack/react-router"
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

function ReadOnlyBanner() {
  return (
    <div className="flex items-center justify-center gap-2 border-b border-amber-500/30 bg-amber-500/10 px-4 py-1.5 font-mono text-2xs uppercase tracking-[0.14em] text-amber-700 dark:text-amber-400">
      read-only
      <Link to="/settings" className="underline underline-offset-2">
        change in settings
      </Link>
    </div>
  )
}

function AppLayout() {
  const readOnly = useReadOnly()
  return (
    <SidebarProvider>
      <ConsoleSidebar />
      <SidebarInset>
        {readOnly && <ReadOnlyBanner />}
        <Outlet />
      </SidebarInset>
    </SidebarProvider>
  )
}
