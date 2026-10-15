import { Button } from "@/components/ui/button"
import { formatRelative, shortenId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { operationPhaseTone } from "@/modules/machinery/display"
import type { Operation } from "@/modules/machinery/types"
import { Link } from "@tanstack/react-router"
import { Panel, PanelEmpty } from "@/components/layout/panel"

const DOT_TONE = {
  danger: "bg-destructive",
  warn: "bg-amber-500",
  info: "bg-primary",
  ok: "bg-primary",
  neutral: "bg-muted-foreground",
} as const

/**
 * What was asked of the fleet, newest first — the second question after what is
 * broken. Every phase, not just the open ones: an operation that failed four
 * minutes ago is the most likely explanation for a panel above.
 */
export function RecentOperations({
  operations,
  isLoading,
  error,
}: {
  operations: Operation[] | undefined
  isLoading: boolean
  error: unknown
}) {
  const rows = operations
    ? [...operations].sort((a, b) => b.created_at.localeCompare(a.created_at))
    : undefined

  return (
    <Panel
      title="Recent operations"
      description={`newest ${rows?.length ?? 0} · every phase`}
      isLoading={isLoading}
      error={error}
      errorMessage="Failed to load operations"
      skeletonRows={4}
      action={
        <Button asChild variant="outline" size="sm">
          <Link to="/operations">All</Link>
        </Button>
      }
    >
      {rows && rows.length === 0 && (
        <PanelEmpty>Nothing has been asked of the fleet.</PanelEmpty>
      )}

      <div className="flex flex-col">
        {rows?.map((operation, index) => (
          <Link
            key={operation.id}
            to="/operations/$operationId"
            params={{ operationId: operation.id }}
            className={cn(
              "group/op relative ml-0.5 border-l pl-4",
              index === rows.length - 1 ? "border-transparent" : "pb-3.5"
            )}
          >
            <span
              className={cn(
                "absolute top-1 -left-[0.28rem] size-2 rounded-full",
                DOT_TONE[operationPhaseTone(operation.phase)]
              )}
            />
            <span className="block font-mono text-2xs text-muted-foreground">
              {formatRelative(operation.created_at)} · {operation.phase}
            </span>
            <span className="mt-0.5 block text-xs leading-snug group-hover/op:underline">
              <span className="font-mono">{operation.type}</span>{" "}
              {operation.target.type}{" "}
              <span className="font-medium">
                {shortenId(operation.target.id)}
              </span>
              {(operation.error ?? operation.reason) && (
                <span className="text-muted-foreground">
                  {" "}
                  — {operation.error ?? operation.reason}
                </span>
              )}
            </span>
          </Link>
        ))}
      </div>
    </Panel>
  )
}
