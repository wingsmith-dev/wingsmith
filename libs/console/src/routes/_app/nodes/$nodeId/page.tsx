import { Field, FieldGrid } from "@/components/layout/field"
import { InFlightOperations } from "@/components/machinery/in-flight"
import { MetricsSection } from "@/components/metrics/metrics-section"
import { OperationActions } from "@/components/machinery/operation-actions"
import { DataTable } from "@/components/layout/data-table"
import { Dash, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { CordonState, NodeConditionLabel } from "@/components/supply/indicators"
import { deviceColumns } from "@/components/supply/columns"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { gibibytes, unhealthyCount } from "@/modules/supply/display"
import { useOpenOperations } from "@/modules/machinery/queries"
import { useDevices, useNode } from "@/modules/supply/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { TopologyCard } from "./TopologyCard"
import { NodeOperations } from "./NodeOperations"

export function NodeDetailPage() {
  const { nodeId } = useParams({ from: "/_app/nodes/$nodeId" })
  const { data: node, isLoading, error } = useNode(nodeId)
  const devices = useDevices({ node: nodeId })
  const inFlight = useOpenOperations(node?.id)

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

            <MetricsSection scope="node" refId={node.id} />

            <div className="flex flex-wrap items-center gap-3">
              <NodeConditionLabel condition={node.condition} />
              <CordonState cordoned={node.cordoned} />
              <span className="font-mono text-2xs text-muted-foreground/60">
                observed {formatRelative(node.observed_at)}
              </span>
              <Labels labels={node.labels} />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">
                    Membership &amp; host
                  </CardTitle>
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
                    <Field label="Devices">
                      {node.devices.total} total ·{" "}
                      {unhealthyCount(node.devices)} unhealthy
                    </Field>
                    <Field label="By health">
                      {Object.entries(node.devices.by_health)
                        .map(([health, count]) => `${health} ${count}`)
                        .join(" · ") || "—"}
                    </Field>
                    <Field label="CPU / RAM">
                      {node.cpu_cores ?? "—"} cores ·{" "}
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
                    <Field label="Agent">
                      <Dash>{node.agent_version}</Dash>
                    </Field>
                    <Field label="Joined">
                      {node.joined_at ? formatAbsolute(node.joined_at) : "—"}
                    </Field>
                    <Field label="Taints">
                      {node.taints.length
                        ? `${node.taints.length} taint(s)`
                        : "none"}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <TopologyCard nodeId={node.id} />
            </div>

            <div>
              <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
                Devices
              </h2>
              <DataTable
                columns={deviceColumns({ showNode: false })}
                list={devices}
                rowKey={(device) => device.id}
                empty="This node reports no devices."
              />
            </div>

            <NodeOperations node={node} />
          </>
        )}
      </div>
    </div>
  )
}
