import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Link, type LinkProps } from "@tanstack/react-router"
import { STAT_TONE_CLASS, type StatTone } from "./stat-tone"

const SIZE_CLASS = {
  sm: "font-mono text-sm leading-none tabular-nums",
  md: "font-mono text-xl leading-none tabular-nums",
  lg: "font-heading text-2xl leading-none font-semibold tabular-nums",
} as const

/**
 * The headline strip a detail page opens with. Five columns at full width; a page
 * with fewer facts leaves the row short rather than padding it.
 */
export function StatRow({
  className,
  children,
}: React.PropsWithChildren<{ className?: string }>) {
  return (
    <div
      className={cn(
        "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5",
        className
      )}
    >
      {children}
    </div>
  )
}

/**
 * One labelled number. The only tile in the console: a page that needs a
 * headline figure uses this rather than a local Card.
 *
 * `detail` sits small beside the value (a denominator), `footnote` on its own
 * line under it, and `children` under that (a badge, a bar, status notes).
 * `isLoading` and `error` are per-tile, so a tile whose query failed says so
 * and the rest of the row still answers its question. `to` makes the whole
 * tile a link to the list the number was counted from.
 */
export function StatTile({
  label,
  value,
  detail,
  footnote,
  tone = "default",
  size = "md",
  icon,
  to,
  isLoading = false,
  error,
  className,
  children,
}: React.PropsWithChildren<{
  label: string
  value?: React.ReactNode
  detail?: React.ReactNode
  /** Pass `null` rather than leaving it out to keep the line's height while loading. */
  footnote?: React.ReactNode
  tone?: StatTone
  size?: keyof typeof SIZE_CLASS
  icon?: React.ReactNode
  to?: LinkProps["to"]
  isLoading?: boolean
  error?: unknown
  className?: string
}>) {
  const loaded = !isLoading && !error
  const body = (
    <>
      <div className="flex items-center gap-2 font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        {icon}
        {label}
      </div>
      {isLoading ? (
        <Skeleton className="h-7 w-20" />
      ) : error ? (
        <div className="text-xs text-destructive">
          {formatError(error, "unavailable")}
        </div>
      ) : (
        value !== undefined && (
          <div className="flex items-baseline gap-1.5">
            <span className={cn(SIZE_CLASS[size], STAT_TONE_CLASS[tone])}>
              {value}
            </span>
            {detail && (
              <span className="font-mono text-2xs text-muted-foreground">
                {detail}
              </span>
            )}
          </div>
        )
      )}
      {footnote !== undefined && (
        <div className="min-h-4 font-mono text-2xs text-muted-foreground">
          {loaded && footnote}
        </div>
      )}
      {loaded && children}
    </>
  )

  if (!to) {
    return (
      <Card size="sm" className={cn("gap-1.5 px-3", className)}>
        {body}
      </Card>
    )
  }
  return (
    <Card
      size="sm"
      className={cn("transition-colors hover:ring-foreground/25", className)}
    >
      <Link to={to} className="flex flex-col gap-1.5 px-3">
        {body}
      </Link>
    </Card>
  )
}
