import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { TableLoading, TableMessage } from "./table-states"
import { formatError } from "@/lib/format"
import { ACTIVATION_FOCUS, activationProps } from "@/lib/keyboard"
import { cn } from "@/lib/utils"

export interface Column<T> {
  key: string
  header: React.ReactNode
  align?: "right"
  /** Hidden below `lg`, for columns that are context rather than the answer. */
  secondary?: boolean
  cell: (row: T) => React.ReactNode
}

export interface ListState<T> {
  items: T[] | undefined
  loaded: number
  isLoading: boolean
  error: unknown
  hasNextPage: boolean
  isFetchingNextPage: boolean
  fetchNextPage: () => void
}

interface DataTableProps<T> {
  columns: Column<T>[]
  list: ListState<T>
  rowKey: (row: T) => string
  empty: string
  onRowClick?: (row: T) => void
  /**
   * Drop the frame. A table already inside a card is a box drawn in a box, and
   * the card's own edge is the only one the eye needs.
   */
  bare?: boolean
}

/**
 * One table for every collection, with the cursor wired in. A list is never
 * complete until `next_cursor` is null, so the footer says what has been loaded
 * rather than letting the first page pass for the whole collection.
 */
export function DataTable<T>({
  columns,
  list,
  rowKey,
  empty,
  onRowClick,
  bare,
}: DataTableProps<T>) {
  const { items, isLoading, error, hasNextPage, isFetchingNextPage } = list

  return (
    <div className="space-y-2">
      <div
        className={cn("overflow-x-auto", !bare && "rounded-lg border bg-card")}
      >
        <Table>
          <TableHeader>
            <TableRow>
              {columns.map((column) => (
                <TableHead
                  key={column.key}
                  className={cellClass(column, "whitespace-nowrap")}
                >
                  {column.header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && <TableLoading columns={columns.length} />}

            {!!error && (
              <TableMessage columns={columns.length} tone="danger">
                {formatError(error)}
              </TableMessage>
            )}

            {items?.map((row) => (
              <TableRow
                key={rowKey(row)}
                className={cn(
                  "hover:bg-accent/40",
                  onRowClick && cn("cursor-pointer", ACTIVATION_FOCUS)
                )}
                {...activationProps(
                  onRowClick ? () => onRowClick(row) : undefined
                )}
              >
                {columns.map((column) => (
                  <TableCell key={column.key} className={cellClass(column)}>
                    {column.cell(row)}
                  </TableCell>
                ))}
              </TableRow>
            ))}

            {items && items.length === 0 && !isLoading && (
              <TableMessage columns={columns.length}>{empty}</TableMessage>
            )}
          </TableBody>
        </Table>
      </div>

      {items && items.length > 0 && (
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
      )}
    </div>
  )
}

function cellClass<T>(column: Column<T>, extra = ""): string {
  return [
    column.align === "right" ? "text-right" : "",
    column.secondary ? "hidden lg:table-cell" : "",
    extra,
  ]
    .filter(Boolean)
    .join(" ")
}
