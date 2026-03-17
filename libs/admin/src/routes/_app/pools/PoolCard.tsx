import { Field } from "@/components/layout/field"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { formatRelative } from "@/lib/format"
import { PROVISIONING_SOURCE_LABEL } from "@/modules/supply/display"
import type { Pool } from "@/modules/supply/types"
import { Link } from "@tanstack/react-router"

function PanelHeading({ kind }: { kind: "declared" | "observed" }) {
  return (
    <div className="mb-3 flex items-center gap-2">
      <span className="font-mono text-2xs uppercase tracking-[0.18em] text-muted-foreground/50">
        {kind}
      </span>
      <span className="font-mono text-2xs text-muted-foreground/40">
        {kind === "declared"
          ? "written by the plane"
          : "reported by the cluster"}
      </span>
    </div>
  )
}

/**
 * Declared spec and observed membership sit in two separate panels on purpose:
 * min/max is intent, node and device counts are what the cluster came back with.
 */
export function PoolCard({ pool }: { pool: Pool }) {
  const { declared, observed } = pool
  const withinBounds =
    observed.nodes_total >= declared.min_nodes &&
    observed.nodes_total <= declared.max_nodes
  const span = Math.max(declared.max_nodes, 1)

  return (
    <Card className="overflow-hidden p-0">
      <div className="flex flex-wrap items-baseline gap-3 border-b bg-muted/30 px-4 py-3">
        <Link
          to="/pools/$poolId"
          params={{ poolId: pool.id }}
          className="font-medium hover:underline"
        >
          {pool.name}
        </Link>
        <span className="font-mono text-2xs text-muted-foreground/60">
          {pool.id}
        </span>
        <Link
          to="/clusters/$clusterId"
          params={{ clusterId: pool.cluster_id }}
          className="font-mono text-2xs text-muted-foreground hover:underline"
        >
          {pool.cluster_name}
        </Link>
        {!withinBounds && (
          <Badge
            variant="outline"
            className="ml-auto border-amber-500/40 bg-amber-500/10 font-mono text-2xs text-amber-600 dark:text-amber-400"
          >
            outside declared bounds
          </Badge>
        )}
      </div>

      <div className="grid divide-y md:grid-cols-2 md:divide-x md:divide-y-0">
        <div className="p-4">
          <PanelHeading kind="declared" />
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Device class">{declared.device_class}</Field>
            <Field label="Node shape">{declared.node_shape}</Field>
            <Field label="Devices per node">{declared.devices_per_node}</Field>
            <Field label="Bounds">
              {declared.min_nodes}–{declared.max_nodes} nodes
            </Field>
            <Field label="Provisioning source">
              {PROVISIONING_SOURCE_LABEL[declared.provisioning_source]}
            </Field>
            <Field label="Taints">
              {declared.taints.length ? declared.taints.join(", ") : "none"}
            </Field>
          </div>
        </div>

        <div className="p-4">
          <PanelHeading kind="observed" />
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Nodes ready">
              {observed.nodes_ready}/{observed.nodes_total}
            </Field>
            <Field label="Nodes cordoned">{observed.nodes_cordoned}</Field>
            <Field label="Devices healthy">
              {observed.devices_healthy}/{observed.devices_total}
            </Field>
            <Field label="Devices allocated">
              {observed.devices_allocated}
            </Field>
            <Field label="Topology domains">{observed.topology_domains}</Field>
            <Field label="Last scaled">
              {formatRelative(observed.last_scaled_at)}
            </Field>
          </div>
          <div className="mt-4 space-y-1.5">
            <div className="flex justify-between font-mono text-2xs text-muted-foreground/60">
              <span>membership against bounds</span>
              <span>
                {observed.nodes_total} of {declared.max_nodes} max
              </span>
            </div>
            <Progress
              value={(observed.nodes_total / span) * 100}
              className="h-1.5"
            />
          </div>
        </div>
      </div>
    </Card>
  )
}
