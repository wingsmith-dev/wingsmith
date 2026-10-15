import { ForbiddenError, NetworkError } from "@/lib/api"
import { cn } from "@/lib/utils"
import {
  IconLock,
  IconMapQuestion,
  IconRefresh,
  IconServerOff,
} from "@tabler/icons-react"
import { Link, useRouter } from "@tanstack/react-router"

interface ErrorScreenProps {
  error: unknown
  reset?: () => void
  className?: string
}

/**
 * Fills whatever it is placed in, so the same screen works inside the app shell
 * (sidebar kept), inside a page section, and as the whole window. The caller
 * decides how much room it gets.
 */
export function ErrorScreen({ error, reset, className }: ErrorScreenProps) {
  const isNetwork = error instanceof NetworkError
  const isForbidden = error instanceof ForbiddenError
  const message =
    error instanceof Error
      ? error.message
      : "An unexpected error occurred. Try again."

  const handleReset = () => {
    if (reset) {
      reset()
    } else {
      window.location.reload()
    }
  }

  return (
    <Frame
      className={className}
      icon={
        isForbidden ? (
          <IconLock className="size-8 text-muted-foreground" />
        ) : (
          <IconServerOff className="size-8 text-muted-foreground" />
        )
      }
      title={
        isNetwork
          ? "The plane is unreachable"
          : isForbidden
            ? "Access denied"
            : "Something went wrong"
      }
      message={message}
    >
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
    </Frame>
  )
}

/** The router's error component for any route without its own. */
export function RouteErrorScreen({ error }: { error: unknown }) {
  const router = useRouter()
  return <ErrorScreen error={error} reset={() => router.invalidate()} />
}

/** The router's answer to a path no route matches. */
export function NotFoundScreen({ className }: { className?: string }) {
  return (
    <Frame
      className={className}
      icon={<IconMapQuestion className="size-8 text-muted-foreground" />}
      title="Page not found"
      message="Nothing in the console lives at this address."
    >
      <Link
        to="/"
        className="inline-flex items-center gap-2 rounded-lg border bg-background px-4 py-2 text-sm font-medium shadow-sm transition-colors hover:bg-secondary"
      >
        Go to the console
      </Link>
    </Frame>
  )
}

function Frame({
  className,
  icon,
  title,
  message,
  children,
}: React.PropsWithChildren<{
  className?: string
  icon: React.ReactNode
  title: string
  message: string
}>) {
  return (
    <div
      role="alert"
      className={cn(
        "flex min-h-[50vh] w-full flex-1 flex-col items-center justify-center p-8",
        className
      )}
    >
      <div className="flex max-w-md flex-col items-center gap-6 text-center">
        <div className="flex size-16 items-center justify-center rounded-2xl border bg-secondary/60">
          {icon}
        </div>
        <div className="space-y-2">
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          <p className="text-sm text-muted-foreground">{message}</p>
        </div>
        {children}
      </div>
    </div>
  )
}
