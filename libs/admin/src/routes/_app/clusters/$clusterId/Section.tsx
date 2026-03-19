import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { Link, type LinkProps } from "@tanstack/react-router"

interface SectionProps {
  title: string
  description?: string
  /** The list this section was counted from, already scoped to the cluster. */
  link?: { props: LinkProps; label: string }
  isLoading: boolean
  error: unknown
  errorMessage: string
  skeletonRows?: number
  children: React.ReactNode
}

/**
 * The overview panel, with a cluster-scoped link on it. Loading and failure are
 * per section: a sub-query that fails costs its own panel and leaves the rest
 * of the tab answering its question.
 */
export function Section({
  title,
  description,
  link,
  isLoading,
  error,
  errorMessage,
  skeletonRows = 4,
  children,
}: SectionProps) {
  return (
    <Card className="gap-0 p-0 py-0">
      <div className="flex flex-wrap items-baseline gap-3 border-b px-4 py-3">
        <h2 className="font-heading text-sm font-medium">{title}</h2>
        {description && (
          <p className="font-mono text-2xs text-muted-foreground/60">
            {description}
          </p>
        )}
        {link && (
          <Link
            {...link.props}
            className="ml-auto font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
          >
            {link.label} →
          </Link>
        )}
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

export function SectionEmpty({ children }: React.PropsWithChildren) {
  return (
    <p className="p-6 text-center text-xs text-muted-foreground">{children}</p>
  )
}
