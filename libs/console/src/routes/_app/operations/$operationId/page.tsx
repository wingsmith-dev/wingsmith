import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { operationPhaseTone } from "@/modules/machinery/display"
import { useCancelOperation, useOperation } from "@/modules/machinery/queries"
import { isOpen } from "@/modules/machinery/types"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { toast } from "sonner"

function Json({ value }: { value: unknown }) {
  return (
    <pre className="overflow-x-auto rounded-md border bg-muted/30 p-3 font-mono text-2xs">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

/**
 * Polling a drain. The page re-reads the operation while it is unfinished and
 * stops once it reaches a terminal phase, because that is where the observed
 * half stops moving.
 */
export function OperationDetailPage() {
  const { operationId } = useParams({ from: "/_app/operations/$operationId" })
  const { data: operation, isLoading, error } = useOperation(operationId)
  const cancel = useCancelOperation()

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Operation" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load operation")}
        </div>
      </div>
    )
  }

  const open = operation ? isOpen(operation.phase) : false

  return (
    <div className="flex flex-col">
      <PageHeader
        title={operation?.type ?? "Operation"}
        description={operation?.id}
        actions={
          <div className="flex gap-2">
            {operation && open && (
              <Button
                variant="outline"
                disabled={cancel.isPending || !!operation.cancel_requested_at}
                onClick={() =>
                  cancel.mutate(operation.id, {
                    onSuccess: (next) =>
                      toast.success(
                        next.phase === "cancelled"
                          ? "Operation cancelled"
                          : "Cancellation requested"
                      ),
                    onError: (err) =>
                      toast.error(formatError(err, "Could not cancel"), {
                        description:
                          "The plane may still have applied it — this page re-reads the phase.",
                      }),
                  })
                }
              >
                {operation.cancel_requested_at ? "Cancelling…" : "Cancel"}
              </Button>
            )}
            <Button asChild variant="outline">
              <Link to="/operations">
                <IconArrowLeft className="size-4" />
                All operations
              </Link>
            </Button>
          </div>
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {operation && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <StatusLabel
                tone={operationPhaseTone(operation.phase)}
                pulse={operation.phase === "running"}
              >
                {operation.phase}
              </StatusLabel>
              {open && (
                <span className="font-mono text-2xs text-muted-foreground/60">
                  polling every few seconds
                </span>
              )}
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Declared</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Type">{operation.type}</Field>
                    <Field label="Target">
                      {operation.target.type} · {operation.target.id}
                    </Field>
                    <Field label="Requested by">{operation.requested_by}</Field>
                    <Field label="Reason">
                      <Dash>{operation.reason}</Dash>
                    </Field>
                    <Field label="Cluster">
                      {operation.cluster_id ? (
                        <Link
                          to="/clusters/$clusterId"
                          params={{ clusterId: operation.cluster_id }}
                          className="hover:underline"
                        >
                          {operation.cluster_id}
                        </Link>
                      ) : (
                        "—"
                      )}
                    </Field>
                    <Field label="Deadline">
                      {operation.deadline_at
                        ? formatAbsolute(operation.deadline_at)
                        : "none"}
                    </Field>
                    <Field label="Created">
                      {formatAbsolute(operation.created_at)}
                    </Field>
                    <Field label="Cancellation">
                      {operation.cancel_requested_at
                        ? formatRelative(operation.cancel_requested_at)
                        : "not requested"}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Observed</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Phase">{operation.phase}</Field>
                    <Field label="Attempts">{operation.attempts}</Field>
                    <Field label="Started">
                      {operation.started_at
                        ? formatRelative(operation.started_at)
                        : "not started"}
                    </Field>
                    <Field label="Finished">
                      {operation.finished_at
                        ? formatRelative(operation.finished_at)
                        : "—"}
                    </Field>
                    <Field label="Updated">
                      {formatRelative(operation.updated_at)}
                    </Field>
                  </FieldGrid>
                  {operation.error && (
                    <p className="mt-3 font-mono text-xs break-words text-destructive">
                      {operation.error}
                    </p>
                  )}
                </CardContent>
              </Card>

              {Object.keys(operation.parameters).length > 0 && (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Parameters</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <Json value={operation.parameters} />
                  </CardContent>
                </Card>
              )}

              {operation.result && (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Result</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <Json value={operation.result} />
                  </CardContent>
                </Card>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
