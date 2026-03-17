import { Field, FieldGrid } from "@/components/layout/field"
import { PageHeader } from "@/components/layout/page-header"
import { CordonState, NodeConditionLabel } from "@/components/supply/indicators"
import { DevicesTable } from "@/components/supply/devices-table"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatRelative } from "@/lib/format"
import { useNode, useNodeDevices } from "@/modules/supply/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { TopologyCard } from "./TopologyCard"

export function NodeDetailPage() {
  const { nodeId } = useParams({ from: "/_app/nodes/$nodeId" })
  const { data: node, isLoading, error } = useNode(nodeId)
  const devices = useNodeDevices(nodeId)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Node" />
        <div className="p-10 text-center text-sm text-destructive">
          {error instanceof Error ? error.message : "Failed to load node"}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={node?.name ?? "Node"}
        description={node ? `${node.id} · ${node.instance_type}` : undefined}
        actions={
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
            <div className="flex flex-wrap items-center gap-3">
              <NodeConditionLabel condition={node.condition} />
              <CordonState cordoned={node.cordoned} />
              <span className="font-mono text-2xs text-muted-foreground/60">
                last seen {formatRelative(node.last_seen_at)}
              </span>
              <div className="flex flex-wrap gap-1">
                {Object.entries(node.labels).map(([key, value]) => (
                  <Badge
                    key={key}
                    variant="outline"
                    className="font-mono text-2xs"
                  >
                    {key}={value}
                  </Badge>
                ))}
              </div>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Membership &amp; host</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Cluster">
                      <Link
                        to="/clusters/$clusterId"
                        params={{ clusterId: node.cluster_id }}
                        className="hover:underline"
                      >
                        {node.cluster_name}
                      </Link>
                    </Field>
                    <Field label="Pool">
                      {node.pool_id ? (
                        <Link
                          to="/pools/$poolId"
                          params={{ poolId: node.pool_id }}
                          className="hover:underline"
                        >
                          {node.pool_name}
                        </Link>
                      ) : (
                        "unpooled"
                      )}
                    </Field>
                    <Field label="Device class">
                      {node.device_class ?? "—"}
                    </Field>
                    <Field label="Devices healthy">
                      {node.devices_healthy}/{node.device_count} ·{" "}
                      {node.devices_allocated} allocated
                    </Field>
                    <Field label="CPU / RAM">
                      {node.cpu_cores} cores · {node.memory_gb} GB
                    </Field>
                    <Field label="Driver / kernel">
                      {node.driver_version ?? "—"} · {node.kernel_version ?? "—"}
                    </Field>
                    <Field label="Joined">{formatAbsolute(node.joined_at)}</Field>
                    <Field label="Taints">
                      {node.taints.length ? node.taints.join(", ") : "none"}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <TopologyCard domains={node.topology_domains} />
            </div>

            <DevicesTable
              devices={devices.data?.items}
              isLoading={devices.isLoading}
              error={devices.error}
              showNode={false}
              empty="This node reports no devices."
            />
          </>
        )}
      </div>
    </div>
  )
}
