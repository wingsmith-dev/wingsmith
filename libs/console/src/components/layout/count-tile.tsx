import {
  ResourceIcon,
  type ResourceKind,
} from "@/components/layout/resource-icon"
import { StatusDot, type StatusTone } from "@/components/layout/status-dot"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { Link, type LinkProps } from "@tanstack/react-router"

interface TileNote {
  label: string
  tone: StatusTone
}

interface CountTileProps {
  kind: ResourceKind
  label: string
  value: React.ReactNode
  /** The denominator or unit, shown small beside the value. */
  detail?: string
  notes?: TileNote[]
  to: LinkProps["to"]
  isLoading: boolean
  error: unknown
}

/**
 * One headline number, and the link to the list it was counted from. Loading
 * and failure are per-tile: a tile whose query failed says so and the rest of
 * the page still answers its question.
 */
export function CountTile({
  kind,
  label,
  value,
  detail,
  notes,
  to,
  isLoading,
  error,
}: CountTileProps) {
  return (
    <Card
      size="sm"
      className="gap-2 transition-colors hover:ring-foreground/20"
    >
      <Link to={to} className="flex flex-col gap-2 px-3">
        <div className="flex items-center gap-2 font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          <ResourceIcon kind={kind} className="size-3.5" />
          {label}
        </div>

        {isLoading ? (
          <Skeleton className="h-7 w-20" />
        ) : error ? (
          <div className="text-xs text-destructive">
            {formatError(error, "unavailable")}
          </div>
        ) : (
          <div className="flex items-baseline gap-1.5">
            <span className="font-heading text-2xl leading-none font-semibold tabular-nums">
              {value}
            </span>
            {detail && (
              <span className="font-mono text-2xs text-muted-foreground">
                {detail}
              </span>
            )}
          </div>
        )}

        <div className="flex min-h-4 flex-wrap items-center gap-x-3 gap-y-1">
          {!isLoading &&
            !error &&
            notes?.map((note) => (
              <span
                key={note.label}
                className="inline-flex items-center gap-1.5 font-mono text-2xs text-muted-foreground"
              >
                <StatusDot tone={note.tone} />
                {note.label}
              </span>
            ))}
        </div>
      </Link>
    </Card>
  )
}
