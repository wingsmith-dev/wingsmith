import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"
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
 * One labelled number, not a link. `children` render under the value, or in its
 * place when there is no value (a badge, a bar).
 */
export function StatTile({
  label,
  value,
  tone = "default",
  size = "md",
  className,
  children,
}: React.PropsWithChildren<{
  label: string
  value?: React.ReactNode
  tone?: StatTone
  size?: keyof typeof SIZE_CLASS
  className?: string
}>) {
  return (
    <Card size="sm" className={cn("gap-1.5 px-3", className)}>
      <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        {label}
      </div>
      {value !== undefined && (
        <div className={cn(SIZE_CLASS[size], STAT_TONE_CLASS[tone])}>
          {value}
        </div>
      )}
      {children}
    </Card>
  )
}
