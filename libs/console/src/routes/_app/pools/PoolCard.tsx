import { DeviceClassLabel } from "@/components/supply/device-class"
import { RowActions } from "@/components/layout/destructive"
import { Field } from "@/components/layout/field"
import { Labels } from "@/components/layout/mono"
import { ResourceGlyph } from "@/components/layout/resource-icon"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { formatRelative } from "@/lib/format"
import type { Pool } from "@/modules/supply/types"
import { Link } from "@tanstack/react-router"

function PanelHeading({ kind }: { kind: "declared" | "observed" }) {
  return (
    <div className="mb-3 flex items-center gap-2">
      <span className="font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
        {kind}
      </span>
      <span className="font-mono text-2xs text-muted-foreground">
        {kind === "declared"
          ? "written by the plane"
          : "reported by the cluster"}
      </span>
    </div>
  )
}

function shapeLabel(shape: Record<string, unknown>): string {
  const sku = shape.sku
  if (typeof sku === "string") return sku
  const entries = Object.entries(shape)
  if (entries.length === 0) return "—"
  return entries.map(([k, v]) => `${k}=${String(v)}`).join(" ")
}

/**
 * Two panels, never one. min/max is intent; node and device counts are what the
 * cluster came back with. `within_declared_bounds` is the plane's own verdict
 * instead of a comparison re-derived here, so the console and `hgr` cannot
 * disagree about whether a pool is outside what was asked for.
 */
export function PoolCard({
  pool,
  onEdit,
  onDelete,
}: {
  pool: Pool
  onEdit?: () => void
  onDelete?: () => void
}) {
  const { membership } = pool
  const span = Math.max(pool.max_nodes, 1)

  return (
    <Card className="gap-0 overflow-hidden p-0">
      <div className="flex flex-wrap items-center gap-3 border-b bg-muted/30 px-4 py-3">
        <ResourceGlyph kind="pool" size="sm" />
        <Link
          to="/pools/$poolId"
          params={{ poolId: pool.id }}
          className="font-medium hover:underline"
        >
          {pool.name}
        </Link>
        <span className="font-mono text-2xs text-muted-foreground">
          {pool.id}
        </span>
        <Link
          to="/clusters/$clusterId"
          params={{ clusterId: pool.cluster_id }}
          className="font-mono text-2xs text-muted-foreground hover:underline"
        >
          {pool.cluster_id}
        </Link>
        <div className="ml-auto flex items-center gap-2">
          {!pool.enabled && (
            <Badge variant="outline" className="font-mono text-2xs">
              disabled
            </Badge>
          )}
          {!pool.within_declared_bounds && (
            <Badge
              variant="outline"
              className="border-amber-500/40 bg-amber-500/10 font-mono text-2xs text-warning"
            >
              outside declared bounds
            </Badge>
          )}
          <RowActions
            actions={[
              ...(onEdit ? [{ label: "Edit", onSelect: onEdit }] : []),
              ...(onDelete
                ? [{ label: "Delete", onSelect: onDelete, destructive: true }]
                : []),
            ]}
          />
        </div>
      </div>

      <div className="grid divide-y md:grid-cols-2 md:divide-x md:divide-y-0">
        <div className="p-4">
          <PanelHeading kind="declared" />
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Device class">
              <DeviceClassLabel name={pool.device_class} />
            </Field>
            <Field label="Node shape">{shapeLabel(pool.node_shape)}</Field>
            <Field label="Devices per node">{pool.devices_per_node}</Field>
            <Field label="Bounds">
              {pool.min_nodes}–{pool.max_nodes} nodes
            </Field>
            <Field label="Enabled">{pool.enabled ? "yes" : "no"}</Field>
            <Field label="Taints">
              {pool.taints.length ? `${pool.taints.length} taint(s)` : "none"}
            </Field>
            <Field label="Labels" className="sm:col-span-2">
              <Labels labels={pool.labels} />
            </Field>
          </div>
        </div>

        <div className="p-4">
          <PanelHeading kind="observed" />
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Nodes ready">
              {membership.nodes_ready}/{membership.nodes_total}
            </Field>
            <Field label="Nodes cordoned">{membership.nodes_cordoned}</Field>
            <Field label="Devices healthy">
              {membership.devices_healthy}/{membership.devices_total}
            </Field>
            <Field label="Devices allocatable">
              {membership.devices_allocatable}
            </Field>
            <Field label="Updated">{formatRelative(pool.updated_at)}</Field>
            <Field label="Revision">{pool.revision}</Field>
          </div>
          <div className="mt-4 space-y-1.5">
            <div className="flex justify-between font-mono text-2xs text-muted-foreground">
              <span>membership against bounds</span>
              <span>
                {membership.nodes_total} of {pool.max_nodes} max
              </span>
            </div>
            <Progress
              value={(membership.nodes_total / span) * 100}
              className="h-1.5"
            />
          </div>
        </div>
      </div>
    </Card>
  )
}
