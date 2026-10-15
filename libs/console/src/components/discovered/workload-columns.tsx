import type { Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { formatRelative } from "@/lib/format"
import { discoveryStatusTone, imageTag } from "@/modules/discovered/display"
import type { DiscoveredWorkload } from "@/modules/discovered/types"
import { DecisionActions } from "./decision-actions"

/**
 * What an operator needs to decide from: what it is, where it runs, how much of
 * the fleet it is holding, and whether anyone has looked at it yet. Ready and
 * desired replicas stay in one cell but never merge into one number.
 */
export function workloadColumns(): Column<DiscoveredWorkload>[] {
  return [
    {
      key: "name",
      header: "Workload",
      cell: (workload) => (
        <ResourceCell kind="discovered">
          <div className="font-mono text-xs">
            {workload.name}
            <Id value={workload.namespace} className="ml-2" />
          </div>
          <div className="font-mono text-2xs text-muted-foreground">
            {workload.kind} · <Dash>{imageTag(workload.images)}</Dash>
          </div>
        </ResourceCell>
      ),
    },
    {
      key: "cluster",
      header: "Cluster",
      cell: (workload) => (
        <span className="font-mono text-2xs">{workload.cluster_name}</span>
      ),
    },
    {
      key: "model",
      header: "Model",
      cell: (workload) => (
        <span className="font-mono text-2xs">
          <Dash>{workload.model}</Dash>
        </span>
      ),
    },
    {
      key: "replicas",
      header: "Replicas",
      align: "right",
      cell: (workload) => (
        <span className="font-mono text-2xs">
          {workload.replicas_ready}
          <span className="text-muted-foreground">
            /{workload.replicas_desired}
          </span>
        </span>
      ),
    },
    {
      key: "devices",
      header: "Devices",
      align: "right",
      cell: (workload) => (
        <span className="font-mono text-2xs">
          {workload.devices_total}
          {workload.devices_per_replica > 0 && (
            <span className="text-muted-foreground">
              {" "}
              ({workload.devices_per_replica}/replica)
            </span>
          )}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (workload) => (
        <span className="inline-flex items-center gap-2">
          <StatusLabel tone={discoveryStatusTone(workload.status)}>
            {workload.status}
          </StatusLabel>
          {workload.deployment_id && (
            <Badge variant="outline" className="font-mono text-2xs">
              {workload.deployment_id}
            </Badge>
          )}
        </span>
      ),
    },
    {
      key: "first_seen",
      header: "First seen",
      align: "right",
      secondary: true,
      cell: (workload) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>
            {workload.first_seen_at
              ? formatRelative(workload.first_seen_at)
              : null}
          </Dash>
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (workload) => <DecisionActions workload={workload} />,
    },
  ]
}
