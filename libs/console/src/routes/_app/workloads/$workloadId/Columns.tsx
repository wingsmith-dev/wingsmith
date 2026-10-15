import { IconExternalLink } from "@tabler/icons-react"
import { Link } from "@tanstack/react-router"
import { StatusDot } from "@/components/layout/status-dot"
import { formatRelative } from "@/lib/format"
import type { Endpoint, WorkloadReplicaPlacement } from "@/modules/demand/types"
import { mono } from "./Common"

export const workloadEndpointColumns = [
  {
    key: "name",
    header: "Endpoint",
    cell: (e: Endpoint) => (
      <Link
        to="/endpoints/$endpointId"
        params={{ endpointId: e.id }}
        className="inline-flex items-center gap-1 hover:underline"
      >
        <span className="font-mono text-xs">{e.name}</span>
        <IconExternalLink className="size-3 text-muted-foreground" />
      </Link>
    ),
  },
  {
    key: "status",
    header: "Status",
    cell: (e: Endpoint) => (
      <span className="inline-flex items-center gap-2">
        <StatusDot tone={e.enabled ? "ok" : "neutral"} />
        <span className="text-2xs text-muted-foreground">
          {e.enabled ? "enabled" : "disabled"}
        </span>
      </span>
    ),
  },
  {
    key: "visibility",
    header: "Visibility",
    cell: (e: Endpoint) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {e.visibility}
      </span>
    ),
  },
  {
    key: "updated",
    header: "Updated",
    align: "right" as const,
    cell: (e: Endpoint) => mono(formatRelative(e.updated_at ?? e.created_at)),
  },
]

export const replicaColumns = [
  {
    key: "replica",
    header: "Replica",
    cell: (r: WorkloadReplicaPlacement) =>
      r.replica ? (
        mono(r.replica)
      ) : (
        <span className="font-mono text-2xs text-muted-foreground">
          held (no replica yet)
        </span>
      ),
  },
  {
    key: "allocation",
    header: "Allocation",
    secondary: true,
    cell: (r: WorkloadReplicaPlacement) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {r.allocation}
      </span>
    ),
  },
  {
    key: "cluster",
    header: "Cluster",
    cell: (r: WorkloadReplicaPlacement) => (
      <span className="text-xs">{r.cluster}</span>
    ),
  },
  {
    key: "nodes",
    header: "Nodes",
    cell: (r: WorkloadReplicaPlacement) => (
      <span className="font-mono text-2xs">
        {r.nodes.length ? r.nodes.join(", ") : "—"}
      </span>
    ),
  },
  {
    key: "gpus",
    header: "GPUs",
    align: "right" as const,
    cell: (r: WorkloadReplicaPlacement) => mono(String(r.device_count)),
  },
  {
    key: "status",
    header: "Gang",
    cell: (r: WorkloadReplicaPlacement) => (
      <span className="inline-flex items-center gap-2">
        <StatusDot tone={r.gang_complete ? "ok" : "warn"} />
        <span className="text-2xs text-muted-foreground">
          {r.gang_complete ? "complete" : "partial"}
        </span>
      </span>
    ),
  },
]
