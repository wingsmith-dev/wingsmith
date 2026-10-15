import { STAT_TONE_CLASS, type StatTone } from "@/components/layout/stat-tone"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Link, type LinkProps } from "@tanstack/react-router"

interface StatCardProps {
  label: string
  value: React.ReactNode
  /** The line under the number: the denominator, or what the number excludes. */
  footnote?: React.ReactNode
  tone?: StatTone
  /** The list the number was counted from. Optional; the card reads fine without one. */
  to?: LinkProps["to"]
  isLoading?: boolean
  error?: unknown
}

/**
 * One number that predicts pain, not an inventory count. Loading and failure are
 * per-card: a card whose query failed says so and the rest of the row still
 * answers its question.
 */
export function StatCard({
  label,
  value,
  footnote,
  tone = "default",
  to,
  isLoading = false,
  error,
}: StatCardProps) {
  const body = (
    <>
      <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        {label}
      </span>
      {isLoading ? (
        <Skeleton className="h-7 w-20" />
      ) : error ? (
        <span className="text-xs text-destructive">
          {formatError(error, "unavailable")}
        </span>
      ) : (
        <span
          className={cn(
            "font-mono text-2xl leading-none font-semibold tabular-nums",
            STAT_TONE_CLASS[tone]
          )}
        >
          {value}
        </span>
      )}
      <span className="min-h-4 font-mono text-2xs text-muted-foreground">
        {!isLoading && !error && footnote}
      </span>
    </>
  )

  const className =
    "flex flex-col gap-1.5 rounded-lg bg-card px-4 py-3.5 ring-1 ring-foreground/10"

  if (!to) return <div className={className}>{body}</div>

  return (
    <Link
      to={to}
      className={cn(className, "transition-shadow hover:ring-foreground/25")}
    >
      {body}
    </Link>
  )
}
