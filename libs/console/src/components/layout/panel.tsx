import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"

interface PanelProps {
  id?: string
  title: string
  /** Beside the title: a count, a status. */
  badge?: React.ReactNode
  /** The line under the title: what the panel counts, and over what window. */
  description?: React.ReactNode
  /** The header's right-hand slot — a link out, a tab row, a legend. */
  action?: React.ReactNode
  /** `attention` tints the border, so the inbox reads as the loud panel it is. */
  tone?: "default" | "attention"
  isLoading?: boolean
  error?: unknown
  errorMessage?: string
  /** Rows of skeleton drawn while the panel's query is in flight. */
  skeletonRows?: number
  className?: string
  children: React.ReactNode
}

/**
 * A titled section of a page. Loading and failure are per-panel: a panel whose
 * query failed says so and the rest of the page still answers its question.
 */
export function Panel({
  id,
  title,
  badge,
  description,
  action,
  tone = "default",
  isLoading = false,
  error,
  errorMessage = "Failed to load",
  skeletonRows = 4,
  className,
  children,
}: PanelProps) {
  return (
    <section
      id={id}
      className={cn(
        "flex scroll-mt-20 flex-col gap-3.5 rounded-lg bg-card p-4 ring-1",
        tone === "attention" ? "ring-amber-500/35" : "ring-foreground/10",
        className
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 font-heading text-sm font-semibold">
            {title}
            {badge}
          </h2>
          {description && (
            <div className="mt-0.5 text-2xs text-muted-foreground">
              {description}
            </div>
          )}
        </div>
        {action}
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: skeletonRows }, (_, i) => (
            <Skeleton key={i} className="h-7 w-full" />
          ))}
        </div>
      ) : error ? (
        <p className="py-4 text-center text-xs text-destructive">
          {formatError(error, errorMessage)}
        </p>
      ) : (
        children
      )}
    </section>
  )
}

export function PanelEmpty({ children }: React.PropsWithChildren) {
  return (
    <p className="py-3 text-center text-xs text-muted-foreground">{children}</p>
  )
}
