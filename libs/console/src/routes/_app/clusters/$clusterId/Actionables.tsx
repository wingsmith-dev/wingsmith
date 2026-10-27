import { ResourceIcon } from "@/components/layout/resource-icon"
import { StatusDot, type StatusTone } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import type { Actionable } from "@/modules/overview/summary"
import { IconCheck } from "@tabler/icons-react"
import { Link } from "@tanstack/react-router"

const ROW_TONE: Record<StatusTone, string> = {
  danger: "border-destructive/30 bg-destructive/5 hover:bg-destructive/10",
  warn: "border-amber-500/30 bg-amber-500/5 hover:bg-amber-500/10",
  info: "border-primary/25 bg-primary/5 hover:bg-primary/10",
  ok: "border-emerald-500/30 bg-emerald-500/5 hover:bg-emerald-500/10",
  neutral: "border-border bg-muted/30 hover:bg-muted/50",
}

export interface SourceFailure {
  label: string
  error: unknown
}

/**
 * The foot of the tab, and intentionally not a summary of everything: a row
 * appears only where a human has something to do, and links to the thing it is
 * about. An empty list is the good outcome and is written as one.
 */
export function Actionables({
  actionables,
  isLoading,
  failures,
}: {
  actionables: Actionable[]
  isLoading: boolean
  failures: SourceFailure[]
}) {
  const counts = actionables.reduce<Record<string, number>>((acc, item) => {
    acc[item.tone] = (acc[item.tone] ?? 0) + 1
    return acc
  }, {})

  return (
    <Card className="gap-0 p-0 py-0">
      <div className="flex flex-wrap items-baseline gap-3 border-b px-4 py-3">
        <h2 className="font-heading text-sm font-medium">Needs attention</h2>
        <p className="font-mono text-2xs text-muted-foreground">
          everything on this cluster somebody has to act on
        </p>
        <div className="ml-auto flex items-center gap-3">
          {(["danger", "warn", "info"] as const).map(
            (tone) =>
              !!counts[tone] && (
                <span
                  key={tone}
                  className="inline-flex items-center gap-1.5 font-mono text-2xs text-muted-foreground"
                >
                  <StatusDot tone={tone} />
                  {counts[tone]}
                </span>
              )
          )}
        </div>
      </div>

      {isLoading ? (
        <div className="space-y-2 p-4">
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      ) : actionables.length === 0 ? (
        <div className="flex items-center justify-center gap-3 p-8 text-center">
          <IconCheck className="size-4 text-success" />
          <p className="text-sm text-muted-foreground">
            Nothing to do here. Every pool is within its declared bounds, every
            node and device reports healthy, and nothing is in flight.
          </p>
        </div>
      ) : (
        <div className="space-y-2 p-4">
          {actionables.map((item) => (
            <Link
              key={item.id}
              {...item.link}
              className={`flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border px-3 py-2 ${ROW_TONE[item.tone]}`}
            >
              <StatusDot tone={item.tone} />
              <ResourceIcon
                kind={item.kind}
                className="size-3.5 text-muted-foreground"
              />
              <span className="text-xs font-medium">{item.title}</span>
              {item.badges?.map((badge) => (
                <Badge
                  key={badge}
                  variant="outline"
                  className="font-mono text-2xs"
                >
                  {badge}
                </Badge>
              ))}
              {item.detail && (
                <span className="ml-auto font-mono text-2xs text-muted-foreground">
                  {item.detail}
                </span>
              )}
            </Link>
          ))}
        </div>
      )}

      {failures.length > 0 && (
        <div className="space-y-1 border-t px-4 py-3">
          {failures.map((failure) => (
            <p
              key={failure.label}
              className="font-mono text-2xs text-destructive"
            >
              {failure.label}: {formatError(failure.error, "failed to load")}.
              Anything wrong there is not listed above.
            </p>
          ))}
        </div>
      )}
    </Card>
  )
}
