import { StatusDot } from "@/components/layout/status-dot"
import { formatAbsolute, formatRelative } from "@/lib/format"
import { severityTone } from "@/modules/machinery/display"
import type { PlaneEvent } from "@/modules/machinery/types"
import { Link } from "@tanstack/react-router"
import { Section, SectionEmpty } from "./Section"

/**
 * The newest events for this cluster only: a recent slice by construction, and
 * the one thing on this tab that is not a total.
 */
export function ClusterEvents({
  clusterId,
  events,
  isLoading,
  error,
}: {
  clusterId: string
  events: PlaneEvent[] | undefined
  isLoading: boolean
  error: unknown
}) {
  return (
    <Section
      title="Recent events"
      description="observed · newest first · not a total"
      link={{
        props: { to: "/events", search: { cluster: clusterId } },
        label: "All events",
      }}
      isLoading={isLoading}
      error={error}
      errorMessage="Failed to load events"
    >
      {events && events.length === 0 && (
        <SectionEmpty>No events recorded for this cluster.</SectionEmpty>
      )}

      <div className="divide-y">
        {events?.map((event) => (
          <div key={event.id} className="flex items-baseline gap-3 px-4 py-2.5">
            <StatusDot
              tone={severityTone(event.severity)}
              className="translate-y-1"
            />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <Link
                  to="/events"
                  search={{ cluster: clusterId, kind: event.kind }}
                  className="font-mono text-2xs hover:underline"
                >
                  {event.kind}
                </Link>
                <span className="font-mono text-2xs text-muted-foreground">
                  {event.subject.type}
                </span>
              </div>
              <p className="truncate text-xs text-muted-foreground">
                {event.message}
              </p>
            </div>
            {event.operation_id && (
              <Link
                to="/operations/$operationId"
                params={{ operationId: event.operation_id }}
                className="font-mono text-2xs text-muted-foreground hover:underline"
              >
                operation
              </Link>
            )}
            <span
              className="shrink-0 font-mono text-2xs text-muted-foreground"
              title={formatAbsolute(event.occurred_at)}
            >
              {formatRelative(event.occurred_at)}
            </span>
          </div>
        ))}
      </div>
    </Section>
  )
}
