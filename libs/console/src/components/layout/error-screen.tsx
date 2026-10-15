import { IconLock, IconRefresh, IconServerOff } from "@tabler/icons-react"
import { ForbiddenError, NetworkError } from "@/lib/api"

interface ErrorScreenProps {
  error: unknown
  reset?: () => void
}

export function ErrorScreen({ error, reset }: ErrorScreenProps) {
  const isNetwork = error instanceof NetworkError
  const isForbidden = error instanceof ForbiddenError
  const message =
    error instanceof Error
      ? error.message
      : "An unexpected error occurred. Please try again."

  const handleReset = () => {
    if (reset) {
      reset()
    } else {
      window.location.reload()
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-background p-8">
      <div className="flex max-w-md flex-col items-center gap-6 text-center">
        <div className="flex size-16 items-center justify-center rounded-2xl border bg-secondary/60">
          {isForbidden ? (
            <IconLock className="size-8 text-muted-foreground" />
          ) : (
            <IconServerOff className="size-8 text-muted-foreground" />
          )}
        </div>

        <div className="space-y-2">
          <h1 className="text-xl font-semibold tracking-tight">
            {isNetwork
              ? "System Unavailable"
              : isForbidden
                ? "Access Denied"
                : "Something went wrong"}
          </h1>
          <p className="text-sm text-muted-foreground">{message}</p>
        </div>

        {!isNetwork && !isForbidden && (
          <p className="rounded-lg border bg-secondary/40 px-4 py-2 font-mono text-2xs text-muted-foreground">
            {String(error)}
          </p>
        )}

        {!isForbidden && (
          <button
            onClick={handleReset}
            className="inline-flex items-center gap-2 rounded-lg border bg-background px-4 py-2 text-sm font-medium shadow-sm transition-colors hover:bg-secondary"
          >
            <IconRefresh className="size-4" />
            Try again
          </button>
        )}
      </div>
    </div>
  )
}
