import { StatusLabel } from "@/components/layout/status-dot"
import { formatRelative } from "@/lib/format"
import { operationPhaseTone } from "@/modules/machinery/display"
import type { Operation } from "@/modules/machinery/types"
import { Link } from "@tanstack/react-router"

/**
 * What is already running against this resource, said before the operator gets
 * to the buttons. An operation that returns no visible change is an operation
 * an operator starts three times.
 */
export function InFlightOperations({
  operations,
}: {
  operations: Operation[]
}) {
  if (operations.length === 0) return null

  return (
    <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3">
      <p className="font-mono text-2xs tracking-[0.18em] text-amber-700 uppercase dark:text-amber-400">
        {operations.length} operation{operations.length === 1 ? "" : "s"} in
        flight
      </p>
      <ul className="mt-2 space-y-1">
        {operations.map((operation) => (
          <li key={operation.id} className="flex flex-wrap items-center gap-3">
            <StatusLabel tone={operationPhaseTone(operation.phase)} pulse>
              {operation.phase}
            </StatusLabel>
            <Link
              to="/operations/$operationId"
              params={{ operationId: operation.id }}
              className="font-mono text-xs hover:underline"
            >
              {operation.type}
            </Link>
            <span className="font-mono text-2xs text-muted-foreground/60">
              {operation.id} · requested {formatRelative(operation.created_at)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
