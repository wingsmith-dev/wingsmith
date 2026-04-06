import { Field, FieldGrid } from "@/components/layout/field"
import { Dash, Id, Labels } from "@/components/layout/mono"
import {
  ResourceIcon,
  type ResourceKind,
} from "@/components/layout/resource-icon"
import {
  StatusDot,
  StatusLabel,
  type StatusTone,
} from "@/components/layout/status-dot"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import {
  CLUSTER_PHASE_TONE,
  humanize,
  syncTone,
} from "@/modules/clusters/display"
import type { Cluster } from "@/modules/clusters/types"
import type { Deployment } from "@/modules/demand/types"
import type { DeviceSummary, NodeSummary } from "@/modules/overview/summary"
import type { Pool } from "@/modules/supply/types"
import { Link, type LinkProps } from "@tanstack/react-router"

interface TileNote {
  label: string
  tone: StatusTone
  link?: LinkProps
}

/**
 * One count and the cluster-scoped list it came from. Every number here is
 * drained rather than read off a first page, so it is a collection total.
 */
function Tile({
  kind,
  label,
  value,
  detail,
  notes,
  link,
  isLoading,
  error,
}: {
  kind: ResourceKind
  label: string
  value: React.ReactNode
  detail?: string
  notes?: TileNote[]
  link: LinkProps
  isLoading: boolean
  error: unknown
}) {
  return (
    <Card
      size="sm"
      className="gap-2 transition-colors hover:ring-foreground/20"
    >
      <div className="flex flex-col gap-2 px-3">
        <Link
          {...link}
          className="flex items-center gap-2 font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase hover:text-foreground"
        >
          <ResourceIcon kind={kind} className="size-3.5" />
          {label}
        </Link>

        {isLoading ? (
          <Skeleton className="h-7 w-20" />
        ) : error ? (
          <div className="text-xs text-destructive">
            {formatError(error, "unavailable")}
          </div>
        ) : (
          <div className="flex items-baseline gap-1.5">
            <span className="font-heading text-2xl leading-none font-semibold tabular-nums">
              {value}
            </span>
            {detail && (
              <span className="font-mono text-2xs text-muted-foreground/60">
                {detail}
              </span>
            )}
          </div>
        )}

        <div className="flex min-h-4 flex-wrap items-center gap-x-3 gap-y-1">
          {!isLoading &&
            !error &&
            notes?.map((note) =>
              note.link ? (
                <Link
                  key={note.label}
                  {...note.link}
                  className="inline-flex items-center gap-1.5 font-mono text-2xs text-muted-foreground hover:underline"
                >
                  <StatusDot tone={note.tone} />
                  {note.label}
                </Link>
              ) : (
                <span
                  key={note.label}
                  className="inline-flex items-center gap-1.5 font-mono text-2xs text-muted-foreground"
                >
                  <StatusDot tone={note.tone} />
                  {note.label}
                </span>
              )
            )}
        </div>
      </div>
    </Card>
  )
}

interface GlanceProps {
  cluster: Cluster
  pools: { data: Pool[] | undefined; isLoading: boolean; error: unknown }
  nodes: {
    summary: NodeSummary | undefined
    isLoading: boolean
    error: unknown
  }
  devices: {
    summary: DeviceSummary | undefined
    isLoading: boolean
    error: unknown
  }
  deployments: {
    data: Deployment[] | undefined
    isLoading: boolean
    error: unknown
  }
}

export function GlanceTiles({
  cluster,
  pools,
  nodes,
  devices,
  deployments,
}: GlanceProps) {
  const outOfBounds =
    pools.data?.filter((p) => !p.within_declared_bounds).length ?? 0
  const replicasReady =
    deployments.data?.reduce((sum, d) => sum + d.replicas.ready, 0) ?? 0
  const replicasDesired =
    deployments.data?.reduce((sum, d) => sum + d.desired_replicas, 0) ?? 0

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Tile
        kind="pool"
        label="Pools"
        value={pools.data?.length ?? 0}
        detail="declared"
        link={{ to: "/pools", search: { cluster: cluster.id } }}
        notes={
          pools.data
            ? [
                {
                  label: `${pools.data.filter((p) => p.enabled).length} enabled`,
                  tone: "ok",
                },
                ...(outOfBounds > 0
                  ? [
                      {
                        label: `${outOfBounds} outside bounds`,
                        tone: "warn" as const,
                      },
                    ]
                  : []),
              ]
            : []
        }
        isLoading={pools.isLoading}
        error={pools.error}
      />
      <Tile
        kind="node"
        label="Nodes"
        value={
          nodes.summary ? `${nodes.summary.ready}/${nodes.summary.total}` : 0
        }
        detail="ready"
        link={{ to: "/nodes", search: { cluster: cluster.id } }}
        notes={
          nodes.summary
            ? [
                ...(nodes.summary.notReady > 0
                  ? [
                      {
                        label: `${nodes.summary.notReady} not ready`,
                        tone: "danger" as const,
                        link: {
                          to: "/nodes",
                          search: {
                            cluster: cluster.id,
                            condition: "not_ready",
                          },
                        } as LinkProps,
                      },
                    ]
                  : []),
                ...(nodes.summary.cordoned > 0
                  ? [
                      {
                        label: `${nodes.summary.cordoned} cordoned`,
                        tone: "warn" as const,
                        link: {
                          to: "/nodes",
                          search: { cluster: cluster.id, cordoned: "true" },
                        } as LinkProps,
                      },
                    ]
                  : []),
              ]
            : []
        }
        isLoading={nodes.isLoading}
        error={nodes.error}
      />
      <Tile
        kind="device"
        label="Devices"
        value={
          devices.summary
            ? `${devices.summary.healthy}/${devices.summary.total}`
            : 0
        }
        detail="healthy"
        link={{ to: "/devices", search: { cluster: cluster.id } }}
        notes={
          devices.summary
            ? [
                {
                  label: `${devices.summary.allocatable} allocatable`,
                  tone: "ok",
                  link: {
                    to: "/devices",
                    search: { cluster: cluster.id, allocatable: "true" },
                  } as LinkProps,
                },
                ...(devices.summary.impaired.length > 0
                  ? [
                      {
                        label: `${devices.summary.impaired.length} need attention`,
                        tone: "warn" as const,
                      },
                    ]
                  : []),
              ]
            : []
        }
        isLoading={devices.isLoading}
        error={devices.error}
      />
      <Tile
        kind="deployment"
        label="Deployments"
        value={deployments.data?.length ?? 0}
        link={{ to: "/deployments", search: { cluster: cluster.id } }}
        notes={
          deployments.data
            ? [
                {
                  label: `${replicasReady}/${replicasDesired} replicas ready`,
                  tone: replicasReady < replicasDesired ? "warn" : "ok",
                },
              ]
            : []
        }
        isLoading={deployments.isLoading}
        error={deployments.error}
      />
    </div>
  )
}

/**
 * Identity, declared and observed in one card. Phase and reachability sit apart
 * on purpose: a cluster can be declared active and be unreachable at once.
 */
export function ClusterIdentity({ cluster }: { cluster: Cluster }) {
  return (
    <Card className="p-4">
      <FieldGrid>
        <Field label="Cluster">
          {cluster.name}
          <Id value={cluster.id} className="ml-2" />
        </Field>
        <Field label="Region">
          {cluster.region}
          {cluster.zone && ` · ${cluster.zone}`}
        </Field>
        <Field label="Phase">
          <StatusLabel tone={CLUSTER_PHASE_TONE[cluster.phase] ?? "neutral"}>
            {humanize(cluster.phase)}
          </StatusLabel>
        </Field>
        <Field label="Reachability">
          <StatusLabel tone={cluster.reachable ? "ok" : "danger"}>
            {cluster.reachable ? "reachable" : "unreachable"}
          </StatusLabel>
        </Field>
        <Field label="Sync">
          <StatusLabel tone={syncTone(cluster.sync.state)}>
            {humanize(cluster.sync.state)}
          </StatusLabel>
        </Field>
        <Field label="Agent version">
          <Dash>{cluster.sync.agent_version}</Dash>
        </Field>
        <Field label="Last sync">
          <span title={formatAbsolute(cluster.sync.last_sync_at)}>
            {formatRelative(cluster.sync.last_sync_at)}
          </span>
        </Field>
        <Field label="Enrolled">
          {cluster.enrolled_at
            ? formatAbsolute(cluster.enrolled_at)
            : "not enrolled"}
        </Field>
        <Field label="Residency">
          <Dash>{cluster.residency}</Dash>
        </Field>
        <Field label="Capabilities">
          {Object.keys(cluster.capabilities).length === 0 ? (
            <span className="text-muted-foreground/40">none</span>
          ) : (
            Object.entries(cluster.capabilities)
              .map(([key, value]) =>
                value === true ? key : `${key}=${String(value)}`
              )
              .join(" · ")
          )}
        </Field>
        <Field label="Labels" className="sm:col-span-2">
          <Labels labels={cluster.labels} />
        </Field>
      </FieldGrid>
    </Card>
  )
}
