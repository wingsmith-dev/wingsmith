import { Field, FieldGrid } from "@/components/layout/field"
import { InFlightOperations } from "@/components/machinery/in-flight"
import { MetricsSection } from "@/components/metrics/metrics-section"
import { OperationActions } from "@/components/machinery/operation-actions"
import { DataTable } from "@/components/layout/data-table"
import { CopyableId, Dash, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import {
  StatusDot,
  StatusLabel,
  type StatusTone,
} from "@/components/layout/status-dot"
import { CordonState, NodeConditionLabel } from "@/components/supply/indicators"
import { deviceColumns } from "@/components/supply/columns"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  formatAbsolute,
  formatError,
  formatRelative,
} from "@/lib/format"
import { severityTone } from "@/modules/machinery/display"
import type { Allocation, PlaneEvent } from "@/modules/machinery/types"
import { gibibytes, unhealthyCount } from "@/modules/supply/display"
import { useOpenOperations, useAllocations, useEvents } from "@/modules/machinery/queries"
import { useDevices, useNode } from "@/modules/supply/queries"
import { useCluster } from "@/modules/clusters/queries"
import type { Device, SupplyNode } from "@/modules/supply/types"
import { IconArrowLeft, IconArrowUpRight } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { TopologyCard } from "./TopologyCard"
import { NodeOperations } from "./NodeOperations"

export function NodeDetailPage() {
  const { nodeId } = useParams({ from: "/_app/nodes/$nodeId" })
  const { data: node, isLoading, error } = useNode(nodeId)
  const { data: cluster } = useCluster(node?.cluster ?? "")
  // The node's cluster agent is unreachable: its observed condition is the last
  // heartbeat, not a live fact, so readiness reads as stale rather than ready.
  const stale = cluster ? !cluster.reachable : false
  const devices = useDevices({ node: nodeId })
  const inFlight = useOpenOperations(node?.id)
  const allocations = useAllocations({ live: true })
  const events = useEvents({ subject: node?.id ?? "" })

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Node" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load node")}
        </div>
      </div>
    )
  }

  const deviceItems = devices.items ?? []
  /** Live allocations holding at least one device on this node. */
  const nodeAllocations = (allocations.items ?? []).filter((alloc) =>
    alloc.devices.some((d) => d.live && d.node === node?.id)
  )
  const eccTotal = deviceItems.reduce(
    (sum, d) => sum + d.ecc_volatile_errors,
    0
  )

  return (
    <div className="flex flex-col">
      <PageHeader
        title={node?.name ?? "Node"}
        description={node ? `${node.id} · ${node.cluster_name}` : undefined}
        actions={
          node && (
            <OperationActions
              target={node.id}
              prefix="nod_"
              context={{ cordoned: node.cordoned, open: inFlight.types }}
            />
          )
        }
        readActions={
          <Button asChild variant="outline" size="sm">
            <Link to="/nodes">
              <IconArrowLeft className="size-4" />
              All nodes
            </Link>
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {node && (
          <>
            <InFlightOperations operations={inFlight.operations} />

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
              <StatTile label="Status">
                <NodeConditionLabel condition={node.condition} stale={stale} />
              </StatTile>
              <StatTile label="Devices">
                <span className="font-mono text-2xl leading-none font-semibold tabular-nums">
                  {node.devices.total - unhealthyCount(node.devices)}/
                  {node.devices.total}
                </span>
              </StatTile>
              <StatTile label="Replicas">
                <span className="font-mono text-2xl leading-none font-semibold tabular-nums">
                  {nodeAllocations.length}
                </span>
              </StatTile>
              <StatTile label="Uptime">
                <span className="font-mono text-sm text-muted-foreground">
                  {node.joined_at ? formatRelative(node.joined_at) : "—"}
                </span>
              </StatTile>
              <StatTile label="ECC errors">
                <span
                  className={
                    eccTotal > 0
                      ? "font-mono text-2xl leading-none font-semibold tabular-nums text-amber-600 dark:text-amber-400"
                      : "font-mono text-2xl leading-none font-semibold tabular-nums"
                  }
                >
                  {eccTotal}
                </span>
              </StatTile>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <CordonState cordoned={node.cordoned} />
              <span className="font-mono text-2xs text-muted-foreground/60">
                observed {formatRelative(node.observed_at)}
              </span>
            </div>

            <MetricsSection scope="node" refId={node.id} />

            <div className="grid gap-4 lg:grid-cols-3">
              <div className="space-y-4 lg:col-span-2">
                <Card className="gap-0 p-0 py-0">
                  <div className="flex flex-wrap items-baseline gap-3 border-b px-4 py-3">
                    <h2 className="font-heading text-sm font-medium">
                      Devices
                    </h2>
                    <p className="font-mono text-2xs text-muted-foreground/60">
                      {node.devices.total} total ·{" "}
                      {unhealthyCount(node.devices)} unhealthy
                    </p>
                    <Link
                      to="/devices"
                      search={{ node: node.id }}
                      className="ml-auto font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
                    >
                      All devices →
                    </Link>
                  </div>
                  <div className="p-2">
                    <DataTable
                      columns={deviceColumns({ showNode: false })}
                      list={devices}
                      rowKey={(device) => device.id}
                      empty="This node reports no devices."
                    />
                  </div>
                </Card>

                <ReplicasCard
                  node={node}
                  allocations={nodeAllocations}
                  isLoading={allocations.isLoading}
                />

                <ConditionsCard node={node} devices={deviceItems} stale={stale} />
              </div>

              <div className="space-y-4">
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Node</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <FieldGrid>
                      <Field label="Cluster">
                        <Link
                          to="/clusters/$clusterId"
                          params={{ clusterId: node.cluster }}
                          className="hover:underline"
                        >
                          {node.cluster_name}
                        </Link>
                      </Field>
                      <Field label="Pool">
                        {node.pool ? (
                          <Link
                            to="/pools/$poolId"
                            params={{ poolId: node.pool }}
                            className="hover:underline"
                          >
                            {node.pool_name}
                          </Link>
                        ) : (
                          "unpooled"
                        )}
                      </Field>
                      <Field label="Instance">
                        <Dash>{node.instance_type}</Dash>
                      </Field>
                      <Field label="Region">
                        <Dash>
                          {[node.cloud, node.region, node.zone]
                            .filter(Boolean)
                            .join(" · ") || null}
                        </Dash>
                      </Field>
                      {/* TODO-API node schema carries no internal IP */}
                      <Field label="Internal IP">
                        <CopyableId value="10.0.0.0">10.0.0.0</CopyableId>
                      </Field>
                      <Field label="CPU">
                        {node.cpu_cores ?? "—"} cores
                      </Field>
                      <Field label="Host memory">
                        {gibibytes(node.memory_mib)}
                      </Field>
                      <Field label="Architecture">
                        <Dash>{node.cpu_arch}</Dash>
                      </Field>
                      <Field label="OS image">
                        <Dash>{node.os_image}</Dash>
                      </Field>
                      <Field label="Kernel">
                        <Dash>{node.kernel_version}</Dash>
                      </Field>
                      <Field label="Driver">
                        <Dash>{node.driver_version}</Dash>
                      </Field>
                      {/* TODO-API node schema carries no CUDA runtime version */}
                      <Field label="CUDA">
                        <span className="text-muted-foreground/40">—</span>
                      </Field>
                      <Field label="Agent">
                        <Dash>{node.agent_version}</Dash>
                      </Field>
                      <Field label="Joined">
                        {node.joined_at
                          ? formatAbsolute(node.joined_at)
                          : "—"}
                      </Field>
                      <Field label="Taints">
                        {node.taints.length
                          ? `${node.taints.length} taint(s)`
                          : "none"}
                      </Field>
                      <Field label="Labels" className="sm:col-span-2">
                        <Labels labels={node.labels} />
                      </Field>
                    </FieldGrid>
                  </CardContent>
                </Card>

                <TopologyCard nodeId={node.id} />

                <NodeEventsCard
                  nodeId={node.id}
                  events={events.items}
                  isLoading={events.isLoading}
                />
              </div>
            </div>

            <NodeOperations node={node} />
          </>
        )}
      </div>
    </div>
  )
}

function StatTile({
  label,
  children,
}: React.PropsWithChildren<{ label: string }>) {
  return (
    <Card size="sm" className="gap-2">
      <div className="flex flex-col gap-2 px-3">
        <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
          {label}
        </span>
        <div className="flex min-h-7 items-center">{children}</div>
      </div>
    </Card>
  )
}

/**
 * Live allocations holding a device on this node — the closest the observed
 * model gets to "replicas running here". Derived client-side from
 * `/allocations?live=true` because there is no node-scoped allocation filter.
 */
function ReplicasCard({
  node,
  allocations,
  isLoading,
}: {
  node: SupplyNode
  allocations: Allocation[]
  isLoading: boolean
}) {
  const held = allocations.reduce(
    (sum, a) => sum + a.devices.filter((d) => d.live && d.node === node.id).length,
    0
  )
  return (
    <Card className="gap-0 p-0 py-0">
      <div className="flex flex-wrap items-baseline gap-3 border-b px-4 py-3">
        <h2 className="font-heading text-sm font-medium">
          Replicas on this node
        </h2>
        <p className="font-mono text-2xs text-muted-foreground/60">
          {allocations.length} replicas · {held} devices held
        </p>
      </div>
      {isLoading ? (
        <div className="space-y-2 p-4">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : allocations.length === 0 ? (
        <p className="p-6 text-center text-xs text-muted-foreground">
          No live replicas are placed on this node.
        </p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b text-left font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
              <th className="px-4 py-2 font-medium">Replica</th>
              <th className="px-4 py-2 font-medium">Workload</th>
              <th className="px-4 py-2 font-medium">Class</th>
              <th className="px-4 py-2 text-right font-medium">Devices</th>
              <th className="px-4 py-2 font-medium">Acquired</th>
              <th className="px-4 py-2 font-medium">Phase</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {allocations.map((alloc) => (
              <tr key={alloc.id}>
                <td className="px-4 py-2.5 font-mono text-2xs">
                  <Dash>{alloc.replica}</Dash>
                </td>
                <td className="px-4 py-2.5">
                  <Link
                    to="/workloads/$workloadId"
                    params={{ workloadId: alloc.workload }}
                    className="inline-flex items-center gap-1 font-mono text-2xs hover:underline"
                  >
                    {alloc.workload}
                    <IconArrowUpRight className="size-3 text-muted-foreground/50" />
                  </Link>
                </td>
                <td className="px-4 py-2.5 font-mono text-2xs text-muted-foreground">
                  {alloc.device_class}
                </td>
                <td className="px-4 py-2.5 text-right font-mono text-xs">
                  {
                    alloc.devices.filter(
                      (d) => d.live && d.node === node.id
                    ).length
                  }
                </td>
                <td className="px-4 py-2.5 font-mono text-2xs text-muted-foreground">
                  {formatRelative(alloc.acquired_at)}
                </td>
                <td className="px-4 py-2.5">
                  <StatusLabel tone={alloc.live ? "ok" : "neutral"}>
                    {alloc.phase}
                  </StatusLabel>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}

interface NodeConditionRow {
  label: string
  tone: StatusTone
  detail: string
}

/**
 * A node condition's status, mapped to a tone without interpreting its `type`.
 * The plane keeps the operator's vocabulary verbatim, so this reads truthiness
 * only: a signal that is asserting (`True`) is worth a warn, one that is clear
 * (`False`) reads ok, and anything else (`Unknown`) is neutral.
 */
function conditionStatusTone(status: string): StatusTone {
  const value = status.toLowerCase()
  if (value === "false") return "ok"
  if (value === "true") return "warn"
  return "neutral"
}

/**
 * The node's observed conditions. The `Ready` summary row carries the
 * stale/unreachable treatment; the per-signal rows below are the real
 * `node.conditions` array as the cluster reported them, and `GpuEccCorrectable`
 * is derived from the node's own devices.
 */
function ConditionsCard({
  node,
  devices,
  stale = false,
}: {
  node: SupplyNode
  devices: Device[]
  stale?: boolean
}) {
  const eccDevices = devices.filter((d) => d.ecc_volatile_errors > 0)
  const rows: NodeConditionRow[] = [
    {
      label: "Ready",
      tone: stale ? "neutral" : node.ready ? "ok" : "danger",
      detail: stale
        ? `stale · ${node.condition.replace(/_/g, " ")} · cluster unreachable`
        : node.condition.replace(/_/g, " "),
    },
    ...node.conditions.map((condition) => ({
      label: condition.type,
      tone: conditionStatusTone(condition.status),
      detail:
        [condition.reason, condition.message].filter(Boolean).join(" · ") ||
        condition.status,
    })),
    {
      label: "GpuEccCorrectable",
      tone: eccDevices.length > 0 ? "warn" : "ok",
      detail:
        eccDevices.length > 0
          ? `${eccDevices.reduce((s, d) => s + d.ecc_volatile_errors, 0)} across ${eccDevices.length} device(s)`
          : "none",
    },
  ]
  return (
    <Card className="gap-0 p-0 py-0">
      <div className="flex flex-wrap items-baseline gap-3 border-b px-4 py-3">
        <h2 className="font-heading text-sm font-medium">Conditions</h2>
        <p className="font-mono text-2xs text-muted-foreground/60">
          reported by the kubelet and the Hangar agent
        </p>
      </div>
      <div className="divide-y">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex items-center justify-between gap-3 px-4 py-2.5"
          >
            <span className="inline-flex items-center gap-2 font-mono text-xs">
              <StatusDot tone={row.tone} />
              {row.label}
              <span className="text-muted-foreground/60">— {row.detail}</span>
            </span>
          </div>
        ))}
      </div>
    </Card>
  )
}

function NodeEventsCard({
  nodeId,
  events,
  isLoading,
}: {
  nodeId: string
  events: PlaneEvent[] | undefined
  isLoading: boolean
}) {
  return (
    <Card className="gap-0 p-0 py-0">
      <div className="flex flex-wrap items-baseline gap-3 border-b px-4 py-3">
        <h2 className="font-heading text-sm font-medium">Recent events</h2>
        <p className="font-mono text-2xs text-muted-foreground/60">
          observed · newest first
        </p>
        <Link
          to="/events"
          search={{ subject: nodeId }}
          className="ml-auto font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
        >
          All events →
        </Link>
      </div>
      {isLoading ? (
        <div className="space-y-2 p-4">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : events && events.length === 0 ? (
        <p className="p-6 text-center text-xs text-muted-foreground">
          No events recorded for this node.
        </p>
      ) : (
        <div className="divide-y">
          {events?.map((event) => (
            <div
              key={event.id}
              className="flex items-baseline gap-3 px-4 py-2.5"
            >
              <StatusDot
                tone={severityTone(event.severity)}
                className="translate-y-1"
              />
              <div className="min-w-0 flex-1">
                <span className="font-mono text-2xs">{event.kind}</span>
                <p className="text-xs text-muted-foreground">
                  {event.message}
                </p>
              </div>
              <span
                className="shrink-0 font-mono text-2xs text-muted-foreground/50"
                title={formatAbsolute(event.occurred_at)}
              >
                {formatRelative(event.occurred_at)}
              </span>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
