import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import type { ListState } from "@/components/layout/data-table"
import { formatError } from "@/lib/format"

/**
 * The card-shaped counterpart to `DataTable`, for the collections that will
 * only ever hold a handful of rows. It carries the same cursor footer, because
 * a small collection is still a paged one and the first page is still not
 * proof there is nothing more.
 */
export function CardGrid<T>({
  list,
  rowKey,
  empty,
  children,
}: {
  list: ListState<T>
  rowKey: (row: T) => string
  empty: string
  children: (row: T) => React.ReactNode
}) {
  const { items, isLoading, error, hasNextPage, isFetchingNextPage } = list

  if (error) {
    return (
      <p className="rounded-lg border bg-card py-10 text-center text-sm text-destructive">
        {formatError(error)}
      </p>
    )
  }

  if (isLoading) {
    return (
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-44 w-full rounded-xl" />
        ))}
      </div>
    )
  }

  if (items && items.length === 0) {
    return (
      <p className="rounded-lg border bg-card py-10 text-center text-sm text-muted-foreground">
        {empty}
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {items?.map((row) => (
          <div key={rowKey(row)}>{children(row)}</div>
        ))}
      </div>

      <div className="flex items-center justify-between gap-4 px-1">
        <span className="font-mono text-2xs text-muted-foreground">
          {list.loaded} loaded{hasNextPage ? " · more available" : ""}
        </span>
        {hasNextPage && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => list.fetchNextPage()}
            disabled={isFetchingNextPage}
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        )}
      </div>
    </div>
  )
}
