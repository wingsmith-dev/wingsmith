import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { formatAbsolute, formatRelative } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { EVENT_SEVERITIES, severityTone } from "@/modules/machinery/display"
import { useEvents } from "@/modules/machinery/queries"
import type { PlaneEvent } from "@/modules/machinery/types"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

const columns: Column<PlaneEvent>[] = [
  {
    key: "occurred",
    header: "Occurred",
    cell: (event) => (
      <span
        className="font-mono text-2xs text-muted-foreground"
        title={formatAbsolute(event.occurred_at)}
      >
        {formatRelative(event.occurred_at)}
      </span>
    ),
  },
  {
    key: "severity",
    header: "Severity",
    cell: (event) => (
      <StatusLabel tone={severityTone(event.severity)}>
        {event.severity}
      </StatusLabel>
    ),
  },
  {
    key: "kind",
    header: "Kind",
    cell: (event) => <span className="font-mono text-xs">{event.kind}</span>,
  },
  {
    key: "subject",
    header: "Subject",
    cell: (event) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {event.subject.type}
        {event.subject.id && (
          <span className="text-muted-foreground"> · {event.subject.id}</span>
        )}
      </span>
    ),
  },
  {
    key: "message",
    header: "Message",
    cell: (event) => <span className="text-xs">{event.message}</span>,
  },
  {
    key: "operation",
    header: "Operation",
    secondary: true,
    cell: (event) =>
      event.operation_id ? (
        <Link
          to="/operations/$operationId"
          params={{ operationId: event.operation_id }}
          className="font-mono text-2xs hover:underline"
        >
          {event.operation_id}
        </Link>
      ) : (
        <span className="font-mono text-2xs text-muted-foreground">—</span>
      ),
  },
  {
    key: "details",
    header: "Details",
    secondary: true,
    cell: (event) =>
      Object.keys(event.details).length === 0 ? (
        <span className="font-mono text-2xs text-muted-foreground">—</span>
      ) : (
        <span className="font-mono text-2xs text-muted-foreground">
          {Object.entries(event.details)
            .map(([key, value]) => `${key}=${String(value)}`)
            .join(" ")}
        </span>
      ),
  },
]

/**
 * Append-only, observed, and newest first: the plane orders by sequence, so
 * following the cursor walks backwards through history instead of reshuffling
 * the page under the reader.
 */
export function EventsPage() {
  const search = useSearch({ from: "/_app/events" })
  const navigate = useNavigate({ from: "/events" })
  const { data: clusters } = useClusterOptions()
  const list = useEvents({
    cluster: search.cluster,
    kind: search.kind,
    severity: search.severity,
    subject: search.subject,
  })

  const kinds = [...new Set((list.items ?? []).map((e) => e.kind))].sort()

  const set = (key: "cluster" | "kind" | "severity") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Events"
        description="observed · append-only, and never edited"
      />

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Cluster"
            value={search.cluster ?? ALL}
            onChange={set("cluster")}
            options={(clusters ?? []).map((cluster) => ({
              value: cluster.id,
              label: clusterLabel(cluster),
            }))}
          />
          <FilterSelect
            label="Severity"
            value={search.severity ?? ALL}
            onChange={set("severity")}
            options={EVENT_SEVERITIES.map((s) => ({ value: s, label: s }))}
          />
          <FilterSelect
            label="Kind"
            value={search.kind ?? ALL}
            onChange={set("kind")}
            options={kinds.map((k) => ({ value: k, label: k }))}
          />
        </div>

        <DataTable
          columns={columns}
          list={list}
          rowKey={(event) => event.id}
          empty="No events recorded."
        />
      </div>
    </div>
  )
}
