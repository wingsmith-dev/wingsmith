import { cn } from "@/lib/utils"
import { formatAbsolute, formatRelative } from "@/lib/format"
import { StatusDot } from "@/components/layout/status-dot"
import type { StatusTone } from "@/components/layout/status-dot"
import type { PlaneEvent } from "@/modules/machinery/types"

/**
 * Everything that has happened to one deployment, oldest last.
 *
 * The strip above it says where a deployment is now; this says how it got
 * there, which is the question the strip cannot answer. A deployment that spent
 * forty-seven minutes waiting for weights and then landed looks identical, once
 * it has landed, to one that came up instantly: the difference only exists in
 * the log.
 *
 * Two writers, one list, and that is intentional. `deployment.created` and
 * `deployment.scaled` are things a person did; `deployment.pending` and
 * `deployment.placed` are things a cluster concluded; `deployment.dispatched`
 * is the plane saying it delivered. Splitting them into separate panels would
 * make the reader do the interleaving, and the interleaving is the point:
 * "we asked, we sent, the cluster waited, the cluster applied" is one story.
 */
export function DeploymentTimeline({
  events,
  loading,
  empty = "Nothing has been recorded against this deployment yet.",
}: {
  events: PlaneEvent[] | undefined
  loading: boolean
  /** What an empty log means here, for a caller whose subject is not a deployment. */
  empty?: string
}) {
  if (loading && !events) {
    return (
      <p className="text-xs text-muted-foreground">Reading the event log…</p>
    )
  }

  if (!events?.length) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
        <StatusDot tone="neutral" />
        {empty}
      </div>
    )
  }

  return (
    <ol className="space-y-0">
      {events.map((event, i) => (
        <li key={event.id} className="flex gap-3">
          <div className="flex flex-col items-center pt-1.5">
            <StatusDot tone={toneFor(event.severity)} />
            {i < events.length - 1 && (
              <span aria-hidden className="mt-1 w-px flex-1 bg-border" />
            )}
          </div>
          <div className="min-w-0 flex-1 pb-4">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span
                className={cn(
                  "font-mono text-2xs",
                  event.severity === "error" && "text-destructive",
                  event.severity === "warning" && "text-warning",
                  event.severity === "info" && "text-muted-foreground"
                )}
              >
                {event.kind}
              </span>
              <span
                className="text-2xs text-muted-foreground"
                title={formatAbsolute(event.occurred_at)}
              >
                {formatRelative(event.occurred_at)}
              </span>
            </div>
            <p className="mt-0.5 text-xs break-words">{event.message}</p>
            <Detail event={event} />
          </div>
        </li>
      ))}
    </ol>
  )
}

/**
 * The one or two details worth showing inline.
 *
 * Not the whole `details` object. It carries the deployment id and name on
 * every row, which the reader is already looking at, and rendering all of it
 * would bury the reason, which is the field that actually says why a
 * deployment is where it is.
 */
function Detail({ event }: { event: PlaneEvent }) {
  const reason = asText(event.details.reason)
  const ready = asText(event.details.ready_replicas)
  const desired = asText(event.details.desired_replicas)
  const showReplicas = ready !== null && desired !== null && desired !== "0"

  if (!reason && !showReplicas) return null

  return (
    <p className="mt-0.5 space-x-2 text-2xs text-muted-foreground">
      {reason && <span className="font-mono">{reason}</span>}
      {showReplicas && (
        <span>
          {ready}/{desired} ready
        </span>
      )}
    </p>
  )
}

function asText(value: unknown): string | null {
  if (typeof value !== "string" || value === "") return null
  return value
}

function toneFor(severity: string): StatusTone {
  if (severity === "error") return "danger"
  if (severity === "warning") return "warn"
  return "info"
}
