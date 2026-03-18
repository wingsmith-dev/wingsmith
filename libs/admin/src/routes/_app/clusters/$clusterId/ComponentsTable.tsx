import { DataTable, type Column } from "@/components/layout/data-table"
import { VersionPair } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { formatRelative } from "@/lib/format"
import { rolloutTone } from "@/modules/machinery/display"
import { useComponents } from "@/modules/machinery/queries"
import type { Component } from "@/modules/machinery/types"

const columns: Column<Component>[] = [
  {
    key: "name",
    header: "Component",
    cell: (component) => (
      <span className="font-mono text-xs">{component.name}</span>
    ),
  },
  {
    key: "kind",
    header: "Kind",
    cell: (component) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {component.kind}
      </span>
    ),
  },
  {
    key: "version",
    header: "Observed / desired",
    cell: (component) => (
      <VersionPair
        desired={component.desired_version}
        observed={component.status.observed_version}
        behind={component.behind}
      />
    ),
  },
  {
    key: "rollout",
    header: "Rollout",
    cell: (component) => (
      <StatusLabel tone={rolloutTone(component.status.rollout_phase)}>
        {component.status.rollout_phase}
      </StatusLabel>
    ),
  },
  {
    key: "healthy",
    header: "Healthy",
    cell: (component) => (
      <StatusLabel tone={component.status.healthy ? "ok" : "danger"}>
        {component.status.healthy ? "yes" : "no"}
      </StatusLabel>
    ),
  },
  {
    key: "behind",
    header: "Behind",
    cell: (component) =>
      component.behind ? (
        <Badge
          variant="outline"
          className="border-amber-500/40 bg-amber-500/10 font-mono text-2xs text-amber-600 dark:text-amber-400"
        >
          behind
        </Badge>
      ) : (
        <span className="font-mono text-2xs text-muted-foreground/40">—</span>
      ),
  },
  {
    key: "message",
    header: "Message",
    secondary: true,
    cell: (component) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {component.status.message ?? "—"}
      </span>
    ),
  },
  {
    key: "observed",
    header: "Observed",
    align: "right",
    cell: (component) => (
      <span className="font-mono text-2xs text-muted-foreground/70">
        {formatRelative(component.status.observed_at)}
      </span>
    ),
  },
]

/**
 * Declared version and observed version as a pair, never merged. A component
 * nobody has reported on is `behind` — silence is not evidence of a finished
 * rollout, and the plane derives that flag so both clients agree on it.
 */
export function ComponentsTable({ clusterId }: { clusterId: string }) {
  const list = useComponents({ cluster: clusterId })
  return (
    <DataTable
      columns={columns}
      list={list}
      rowKey={(component) => component.id}
      empty="No components declared for this cluster."
    />
  )
}
