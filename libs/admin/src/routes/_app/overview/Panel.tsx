import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { Link, type LinkProps } from "@tanstack/react-router"

interface PanelProps {
  title: string
  description?: string
  to: LinkProps["to"]
  linkLabel: string
  isLoading: boolean
  error: unknown
  errorMessage: string
  /** Rows of skeleton drawn while the panel's query is in flight. */
  skeletonRows?: number
  children: React.ReactNode
}

export function Panel({
  title,
  description,
  to,
  linkLabel,
  isLoading,
  error,
  errorMessage,
  skeletonRows = 4,
  children,
}: PanelProps) {
  return (
    <Card className="gap-0 p-0 py-0">
      <div className="flex flex-wrap items-baseline gap-3 border-b px-4 py-3">
        <h2 className="font-heading text-sm font-medium">{title}</h2>
        {description && (
          <p className="font-mono text-2xs text-muted-foreground/60">
            {description}
          </p>
        )}
        <Link
          to={to}
          className="ml-auto font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
        >
          {linkLabel} →
        </Link>
      </div>

      {isLoading ? (
        <div className="space-y-2 p-4">
          {Array.from({ length: skeletonRows }, (_, i) => (
            <Skeleton key={i} className="h-8 w-full" />
          ))}
        </div>
      ) : error ? (
        <p className="p-6 text-center text-xs text-destructive">
          {formatError(error, errorMessage)}
        </p>
      ) : (
        children
      )}
    </Card>
  )
}

export function PanelEmpty({ children }: React.PropsWithChildren) {
  return (
    <p className="p-6 text-center text-xs text-muted-foreground">{children}</p>
  )
}
