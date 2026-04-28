import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable } from "@/components/layout/data-table"
import { PageHeader } from "@/components/layout/page-header"
import { StatusDot } from "@/components/layout/status-dot"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { operationColumns } from "@/components/machinery/operation-columns"
import { Card } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatAbsolute, formatRelative } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import {
  EVENT_SEVERITIES,
  OPERATION_PHASES,
  severityTone,
} from "@/modules/machinery/display"
import {
  useEvents,
  useOperations,
  useOperationsSummary,
} from "@/modules/machinery/queries"
import { OPERATION_TYPES } from "@/modules/machinery/types"
import type { PlaneEvent } from "@/modules/machinery/types"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"
import { useState } from "react"

/**
 * The imperative surface. Drain, cordon, reset, warm, roll and upgrade all live
 * here rather than as flags hiding on the objects they act upon — an operation
 * has an actor, a reason, attempts and a lifecycle, and a status field has none
 * of those. Beside them, the event feed: everything that happened, whether we
 * asked for it or not.
 */
export function OperationsPage() {
  const search = useSearch({ from: "/_app/operations" })
  const navigate = useNavigate({ from: "/operations" })
  const { data: clusters } = useClusterOptions()
  const [view, setView] = useState<"operations" | "events">("operations")
  const [severity, setSeverity] = useState<string | undefined>()

  const list = useOperations({
    cluster: search.cluster,
    type: search.type,
    phase: search.phase,
    target: search.target,
  })

  const events = useEvents({
    cluster: search.cluster,
    severity,
  })

  const summary = useOperationsSummary(search.cluster)

  const set = (key: "cluster" | "type" | "phase") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  const counts = summary.data
  const byPhase = counts?.by_phase ?? {}

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Operations"
        description="imperative · a type and a target, with a lifecycle of its own"
        actions={<NewOperationDialog />}
      />

      <div className="space-y-4 p-4">
        {/* Summary tiles — the plane's read-time rollup, windowed by its clock. */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <SummaryTile
            label="Running"
            value={byPhase.running ?? 0}
            tone="warn"
          />
          <SummaryTile
            label="Queued"
            value={byPhase.pending ?? 0}
            tone="info"
          />
          <SummaryTile
            label="Failed · 24h"
            value={counts?.failed_24h ?? 0}
            tone="danger"
          />
          <SummaryTile
            label="Succeeded · 24h"
            value={counts?.succeeded_24h ?? 0}
            tone="ok"
          />
        </div>

        <Tabs
          value={view}
          onValueChange={(next) => setView(next as typeof view)}
        >
          <TabsList>
            <TabsTrigger value="operations">Operations</TabsTrigger>
            <TabsTrigger value="events">Events</TabsTrigger>
          </TabsList>

          {/* OPERATIONS — things Hangar was asked to do, each with a lifecycle. */}
          <TabsContent value="operations" className="space-y-4">
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
                label="Type"
                value={search.type ?? ALL}
                onChange={set("type")}
                options={Object.keys(OPERATION_TYPES).map((t) => ({
                  value: t,
                  label: t,
                }))}
              />
              <FilterSelect
                label="Phase"
                value={search.phase ?? ALL}
                onChange={set("phase")}
                options={OPERATION_PHASES.map((p) => ({ value: p, label: p }))}
              />
            </div>

            <DataTable
              columns={operationColumns()}
              list={list}
              rowKey={(operation) => operation.id}
              empty="Nothing has been asked of the fleet."
            />

            <p className="px-1 text-2xs text-muted-foreground/60">
              Most operations start from the resource they act on — cordon from
              a node, roll from a deployment. This page is where you come back
              to see whether they finished.
            </p>
          </TabsContent>

          {/* EVENTS — everything that happened, whether we asked for it or not. */}
          <TabsContent value="events" className="space-y-4">
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
                value={severity ?? ALL}
                onChange={(value) =>
                  setSeverity(value === ALL ? undefined : value)
                }
                options={EVENT_SEVERITIES.map((s) => ({ value: s, label: s }))}
              />
            </div>

            <EventFeed
              events={events.items}
              isLoading={events.isLoading}
              hasNextPage={events.hasNextPage}
              isFetchingNextPage={events.isFetchingNextPage}
              onMore={events.fetchNextPage}
            />

            <p className="px-1 text-2xs text-muted-foreground/60">
              Events tagged with an operation id came from something we
              initiated. Untagged ones — a lost heartbeat, an ECC error — are
              the cluster telling us something on its own.
            </p>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

function SummaryTile({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone: "ok" | "warn" | "danger" | "info"
}) {
  const valueClass =
    tone === "danger"
      ? "text-destructive"
      : tone === "ok"
        ? "text-emerald-500"
        : tone === "warn"
          ? "text-amber-500"
          : "text-foreground"
  return (
    <Card size="sm" className="gap-2 px-3">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </span>
      <span
        className={`font-heading text-2xl leading-none font-semibold tabular-nums ${valueClass}`}
      >
        {value}
      </span>
    </Card>
  )
}

function EventFeed({
  events,
  isLoading,
  hasNextPage,
  isFetchingNextPage,
  onMore,
}: {
  events: PlaneEvent[] | undefined
  isLoading: boolean
  hasNextPage: boolean
  isFetchingNextPage: boolean
  onMore: () => void
}) {
  if (isLoading && !events) {
    return (
      <Card className="px-4 py-6 text-center text-2xs text-muted-foreground">
        Loading events…
      </Card>
    )
  }
  if (!events || events.length === 0) {
    return (
      <Card className="px-4 py-6 text-center text-2xs text-muted-foreground">
        Nothing has happened, or nothing was reported.
      </Card>
    )
  }

  return (
    <Card className="gap-0 py-0">
      <ul className="divide-y">
        {events.map((event) => (
          <li
            key={event.id}
            className="grid grid-cols-[5.5rem_0.5rem_minmax(0,1fr)] items-baseline gap-3 px-4 py-2"
          >
            <span
              className="text-right font-mono text-2xs text-muted-foreground/70"
              title={formatAbsolute(event.occurred_at)}
            >
              {formatRelative(event.occurred_at)}
            </span>
            <StatusDot
              tone={severityTone(event.severity)}
              className="self-center"
            />
            <div className="min-w-0 text-xs leading-relaxed">
              <span>{event.message}</span>
              <div className="mt-0.5 flex flex-wrap items-center gap-2">
                <span className="font-mono text-2xs text-muted-foreground">
                  {event.subject.type}
                  {event.subject.id && (
                    <span className="text-muted-foreground/50">
                      /{event.subject.id}
                    </span>
                  )}
                </span>
                {event.operation_id && (
                  <Link
                    to="/operations/$operationId"
                    params={{ operationId: event.operation_id }}
                    className="font-mono text-2xs text-primary hover:underline"
                  >
                    {event.operation_id}
                  </Link>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
      {hasNextPage && (
        <div className="border-t px-4 py-2 text-center">
          <button
            type="button"
            onClick={onMore}
            disabled={isFetchingNextPage}
            className="font-mono text-2xs text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </Card>
  )
}
