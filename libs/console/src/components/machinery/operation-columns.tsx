import type { Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { formatRelative, shortenId } from "@/lib/format"
import { operationPhaseTone } from "@/modules/machinery/display"
import type { Operation } from "@/modules/machinery/types"
import { Link } from "@tanstack/react-router"

/**
 * The columns an operator reads a stuck drain from: who asked, when, why, and
 * where it got to. A verb endpoint returning 200 could express none of this.
 */
export function operationColumns(): Column<Operation>[] {
  return [
    {
      key: "type",
      header: "Operation",
      cell: (operation) => (
        <ResourceCell kind="operation">
          <Link
            to="/operations/$operationId"
            params={{ operationId: operation.id }}
            className="block font-mono text-xs hover:underline"
          >
            {operation.type}
            <Id value={shortenId(operation.id)} className="ml-2" />
          </Link>
        </ResourceCell>
      ),
    },
    {
      key: "target",
      header: "Target",
      cell: (operation) => (
        <span className="font-mono text-2xs">
          <span className="text-muted-foreground">
            {operation.target.type}{" "}
          </span>
          {operation.target.id}
        </span>
      ),
    },
    {
      key: "phase",
      header: "Phase",
      cell: (operation) => (
        <span className="inline-flex items-center gap-2">
          <StatusLabel
            tone={operationPhaseTone(operation.phase)}
            pulse={operation.phase === "running"}
          >
            {operation.phase}
          </StatusLabel>
          {operation.cancel_requested_at && (
            <Badge variant="outline" className="font-mono text-2xs">
              cancelling
            </Badge>
          )}
        </span>
      ),
    },
    {
      key: "reason",
      header: "Reason",
      cell: (operation) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>{operation.reason}</Dash>
        </span>
      ),
    },
    {
      key: "actor",
      header: "Requested by",
      secondary: true,
      cell: (operation) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {operation.requested_by}
        </span>
      ),
    },
    {
      key: "attempts",
      header: "Attempts",
      align: "right",
      secondary: true,
      cell: (operation) => (
        <span className="font-mono text-2xs">{operation.attempts}</span>
      ),
    },
    {
      key: "created",
      header: "Created",
      align: "right",
      cell: (operation) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(operation.created_at)}
        </span>
      ),
    },
  ]
}
