import type { Column, ListState } from "@/components/layout/data-table"
import { TableLoading, TableMessage } from "@/components/layout/table-states"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatError } from "@/lib/format"
import type { SupplyNode } from "@/modules/supply/types"
import { Link } from "@tanstack/react-router"
import { Fragment, useMemo } from "react"

interface ClusterGroup {
  id: string
  name: string
  nodes: SupplyNode[]
}

/**
 * Groups only what the cursor has actually handed over, in first-seen order, so
 * a group's count is a count of loaded rows and never mistaken for the cluster's
 * node total.
 */
function groupByCluster(nodes: SupplyNode[]): ClusterGroup[] {
  const groups = new Map<string, ClusterGroup>()
  for (const node of nodes) {
    const group = groups.get(node.cluster)
    if (group) {
      group.nodes.push(node)
      continue
    }
    groups.set(node.cluster, {
      id: node.cluster,
      name: node.cluster_name,
      nodes: [node],
    })
  }
  return [...groups.values()]
}

function cellClass(column: Column<SupplyNode>, extra = ""): string {
  return [
    column.align === "right" ? "text-right" : "",
    column.secondary ? "hidden lg:table-cell" : "",
    extra,
  ]
    .filter(Boolean)
    .join(" ")
}

/**
 * The nodes table, sectioned by cluster. A separate rendering rather than a
 * `DataTable` option because grouping is this page's concern and `DataTable` is
 * shared; the states, look and cursor footer are kept identical to it.
 */
export function GroupedNodes({
  columns,
  list,
  empty,
}: {
  columns: Column<SupplyNode>[]
  list: ListState<SupplyNode>
  empty: string
}) {
  const { items, isLoading, error, hasNextPage, isFetchingNextPage } = list
  const groups = useMemo(() => groupByCluster(items ?? []), [items])

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto rounded-lg border bg-card">
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

            {groups.map((group) => (
              <Fragment key={group.id}>
                <TableRow className="hover:bg-transparent">
                  <TableCell
                    colSpan={columns.length}
                    className="sticky top-0 z-10 border-y bg-muted/60 py-2 backdrop-blur-sm"
                  >
                    <span className="flex flex-wrap items-center gap-3">
                      <Link
                        to="/clusters/$clusterId"
                        params={{ clusterId: group.id }}
                        className="font-mono text-2xs tracking-[0.18em] uppercase hover:underline"
                      >
                        {group.name}
                      </Link>
                      <span className="font-mono text-2xs text-muted-foreground">
                        {group.nodes.length} node
                        {group.nodes.length === 1 ? "" : "s"} loaded
                      </span>
                    </span>
                  </TableCell>
                </TableRow>
                {group.nodes.map((node) => (
                  <TableRow key={node.id} className="hover:bg-accent/40">
                    {columns.map((column) => (
                      <TableCell key={column.key} className={cellClass(column)}>
                        {column.cell(node)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </Fragment>
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
            {list.loaded} loaded across {groups.length} cluster
            {groups.length === 1 ? "" : "s"}
            {hasNextPage ? " · more available" : ""}
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
