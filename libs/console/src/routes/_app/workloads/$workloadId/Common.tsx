import { IconChartLine } from "@tabler/icons-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

export function MetricsNotice({ children }: React.PropsWithChildren) {
  return (
    <Card size="sm" className="items-start gap-1 px-3 py-3">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <IconChartLine className="size-4 shrink-0 opacity-60" />
        {children}
      </div>
    </Card>
  )
}

export function EmptyNote({ children }: React.PropsWithChildren) {
  return (
    <p className="py-6 text-center text-2xs text-muted-foreground">
      {children}
    </p>
  )
}

/**
 * A panel for an answer the plane cannot give yet.
 *
 * Kept instead of deleted, because the question is real and the shape of the
 * page is the argument for building it. Drawn as an empty frame with the
 * reason in it instead of as a number, because a designed figure sitting where
 * a measured one goes is indistinguishable from a measurement once somebody
 * screenshots it.
 */
export function Unbacked({ title, note }: { title: string; note: string }) {
  return (
    <Card className="border-dashed">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm text-muted-foreground">
          {title}
          <span className="rounded-full border border-dashed px-1.5 py-0.5 font-mono text-2xs text-muted-foreground">
            not measured yet
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-2xs text-muted-foreground">{note}</p>
      </CardContent>
    </Card>
  )
}

/**
 * A `ListState` wrapper for rows already in hand (a derived table), so they render through the same `DataTable`
 * as the paged, live queries.
 */
export function staticList<T>(items: T[]) {
  return {
    items,
    loaded: items.length,
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: () => {},
  }
}

/* ---- columns ------------------------------------------------------- */

export const mono = (v: string) => (
  <span className="font-mono text-xs">{v}</span>
)

/**
 * The link out of a capped activity panel and into the full log.
 */
export function SeeAllEvents({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      className="font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
      onClick={onClick}
    >
      See all
    </button>
  )
}
