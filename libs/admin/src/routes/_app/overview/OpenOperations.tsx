import { StatusLabel } from "@/components/layout/status-dot"
import { formatRelative } from "@/lib/format"
import { operationPhaseTone } from "@/modules/machinery/display"
import type { Operation } from "@/modules/machinery/types"
import { Link } from "@tanstack/react-router"
import { Panel, PanelEmpty } from "./Panel"

export function OpenOperations({
  operations,
  isLoading,
  error,
}: {
  operations: Operation[] | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Panel
      title="Operations in flight"
      description="pending and running"
      to="/operations"
      linkLabel="All operations"
      isLoading={isLoading}
      error={error}
      errorMessage="Failed to load operations"
      skeletonRows={2}
    >
      {operations && operations.length === 0 && (
        <PanelEmpty>Nothing in flight.</PanelEmpty>
      )}

      <div className="divide-y">
        {operations?.map((operation) => (
          <Link
            key={operation.id}
            to="/operations/$operationId"
            params={{ operationId: operation.id }}
            className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2.5 hover:bg-muted/40"
          >
            <StatusLabel tone={operationPhaseTone(operation.phase)} pulse>
              {operation.phase}
            </StatusLabel>
            <span className="font-mono text-2xs">{operation.type}</span>
            <span className="font-mono text-2xs text-muted-foreground/60">
              {operation.target.type} · {operation.target.id}
            </span>
            <span className="ml-auto font-mono text-2xs text-muted-foreground/50">
              requested {formatRelative(operation.created_at)}
              {operation.attempts > 1 && ` · attempt ${operation.attempts}`}
            </span>
          </Link>
        ))}
      </div>
    </Panel>
  )
}
