import { useState } from "react"
import { createFileRoute, redirect } from "@tanstack/react-router"

import { Button } from "@/components/ui/button"
import { WingsmithMark } from "@/components/layout/wingsmith-mark"
import { ErrorScreen } from "@/components/layout/error-screen"
import { Skeleton } from "@/components/ui/skeleton"
import { useSignOut } from "@/modules/auth/session"
import { useAuthStore } from "@/modules/auth/store"
import { WindowToggle } from "@/components/metrics/window-toggle"
import {
  DEFAULT_SERVING_WINDOW,
  type TimeWindow,
} from "@/modules/metrics/windows"
import { useServingOverview } from "@/modules/serving/queries"
import { ModelCard } from "@/components/serving/model-card"

import { RefreshButton } from "./RefreshButton"

/**
 * The tenant's own view of what it is served. Its own shell, not a page inside
 * the console: everything in that shell (the sidebar, the cluster chrome, the
 * fleet reads on every route) belongs to the operator, and a tenant is not a visitor to
 * it with fewer links.
 *
 * An admin is redirected out, not shown an empty view. The read behind
 * this page takes its tenant from the session, and an admin session names none.
 */
export const Route = createFileRoute("/serving")({
  beforeLoad: () => {
    const session = useAuthStore.getState()
    if (!session.isAuthenticated) {
      throw redirect({ to: "/login" })
    }
    if (session.kind !== "tenant") {
      throw redirect({ to: "/overview" })
    }
  },
  component: ServingPage,
})

function ServingPage() {
  const overview = useServingOverview()
  const signOut = useSignOut()
  const [window, setWindow] = useState<TimeWindow>(DEFAULT_SERVING_WINDOW)

  return (
    <div className="min-h-svh bg-background">
      <header className="flex h-16 items-center justify-between gap-4 border-b bg-card px-6">
        <div className="flex items-center gap-3">
          <div className="flex size-8 items-center justify-center rounded-md bg-foreground text-background">
            <WingsmithMark className="size-4" />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">
              {overview.data?.tenant_name ?? "Serving"}
            </p>
            <p className="text-xs text-muted-foreground">Your models</p>
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={signOut}>
          Sign out
        </Button>
      </header>

      <main className="mx-auto w-full max-w-4xl space-y-4 p-6">
        <div className="flex items-center justify-end gap-2">
          <RefreshButton />
          <WindowToggle value={window} onChange={setWindow} />
        </div>

        {overview.isError ? (
          <ErrorScreen
            error={overview.error}
            reset={() => void overview.refetch()}
          />
        ) : overview.isLoading ? (
          <>
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-40 w-full" />
          </>
        ) : overview.data && overview.data.models.length === 0 ? (
          <p className="rounded-xl border bg-card p-8 text-center text-sm text-muted-foreground">
            No models are published to this account yet.
          </p>
        ) : (
          overview.data?.models.map((model) => (
            <ModelCard key={model.id} model={model} window={window} />
          ))
        )}
      </main>
    </div>
  )
}
