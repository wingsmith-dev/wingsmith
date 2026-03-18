import { Field } from "@/components/layout/field"
import { RowActions } from "@/components/layout/destructive"
import { Id, Labels } from "@/components/layout/mono"
import { ResourceGlyph } from "@/components/layout/resource-icon"
import { StatusLabel } from "@/components/layout/status-dot"
import { Card } from "@/components/ui/card"
import { formatRelative } from "@/lib/format"
import { CLUSTER_PHASE_TONE, clusterLabel, humanize, syncTone } from "@/modules/clusters/display"
import type { Cluster } from "@/modules/clusters/types"
import { Link } from "@tanstack/react-router"

/**
 * Phase and reachability sit next to each other and are never folded into one
 * badge. A cluster that is `active` and unreachable is a network problem; a
 * `disabled` one is somebody's decision, and an operator has to be able to tell
 * them apart at a glance.
 */
export function ClusterCard({
  cluster,
  onEdit,
  onDelete,
}: {
  cluster: Cluster
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <Card className="overflow-hidden p-0">
      <div className="flex items-start gap-3 border-b bg-muted/30 px-4 py-3">
        <ResourceGlyph kind="cluster" size="sm" />
        <div className="min-w-0 flex-1">
          <Link
            to="/clusters/$clusterId"
            params={{ clusterId: cluster.id }}
            className="block truncate font-medium hover:underline"
          >
            {clusterLabel(cluster)}
          </Link>
          <Id value={cluster.name} className="block truncate" />
        </div>
        <RowActions
          actions={[
            { label: "Edit", onSelect: onEdit },
            { label: "Delete", onSelect: onDelete, destructive: true },
          ]}
        />
      </div>

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <StatusLabel tone={CLUSTER_PHASE_TONE[cluster.phase] ?? "neutral"}>
            {humanize(cluster.phase)}
          </StatusLabel>
          <StatusLabel tone={cluster.reachable ? "ok" : "danger"}>
            {cluster.reachable ? "reachable" : "unreachable"}
          </StatusLabel>
          <StatusLabel
            tone={syncTone(cluster.sync.state)}
            pulse={cluster.sync.state === "connected"}
          >
            {humanize(cluster.sync.state)}
          </StatusLabel>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Region">
            {cluster.region}
            {cluster.zone && (
              <span className="text-muted-foreground/50"> · {cluster.zone}</span>
            )}
          </Field>
          <Field label="Agent">{cluster.sync.agent_version ?? "—"}</Field>
          <Field label="Last sync">{formatRelative(cluster.sync.last_sync_at)}</Field>
          <Field label="Enrolled">{formatRelative(cluster.enrolled_at)}</Field>
          <Field label="Labels" className="sm:col-span-2">
            <Labels labels={cluster.labels} />
          </Field>
        </div>
      </div>
    </Card>
  )
}
