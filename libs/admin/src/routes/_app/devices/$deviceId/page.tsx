import { Field, FieldGrid } from "@/components/layout/field"
import { PageHeader } from "@/components/layout/page-header"
import { DeviceHealthLabel } from "@/components/supply/indicators"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatRelative } from "@/lib/format"
import { TOPOLOGY_KIND_LABEL } from "@/modules/supply/display"
import { useDevice } from "@/modules/supply/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"

export function DeviceDetailPage() {
  const { deviceId } = useParams({ from: "/_app/devices/$deviceId" })
  const { data: device, isLoading, error } = useDevice(deviceId)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Device" />
        <div className="p-10 text-center text-sm text-destructive">
          {error instanceof Error ? error.message : "Failed to load device"}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={device?.uuid ?? "Device"}
        description={device ? `${device.id} · ${device.device_class}` : undefined}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to="/devices">
              <IconArrowLeft className="size-4" />
              All devices
            </Link>
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {device && (
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Health</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <DeviceHealthLabel device={device} />
                <FieldGrid>
                  <Field label="ECC volatile">
                    {device.ecc_errors.volatile}
                  </Field>
                  <Field label="ECC aggregate">
                    {device.ecc_errors.aggregate}
                  </Field>
                  <Field label="Throttle reasons">
                    {device.throttle_reasons.length
                      ? device.throttle_reasons.join(", ")
                      : "none"}
                  </Field>
                  <Field label="Last seen">
                    {formatRelative(device.last_seen_at)}
                  </Field>
                </FieldGrid>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Placement</CardTitle>
              </CardHeader>
              <CardContent>
                <FieldGrid>
                  <Field label="Cluster">
                    <Link
                      to="/clusters/$clusterId"
                      params={{ clusterId: device.cluster_id }}
                      className="hover:underline"
                    >
                      {device.cluster_name}
                    </Link>
                  </Field>
                  <Field label="Node">
                    <Link
                      to="/nodes/$nodeId"
                      params={{ nodeId: device.node_id }}
                      className="hover:underline"
                    >
                      {device.node_name}
                    </Link>
                  </Field>
                  <Field label="Index">#{device.index}</Field>
                  <Field label="Memory">
                    {device.memory_used_gb}/{device.memory_gb} GB
                  </Field>
                  <Field label="MIG profile">
                    {device.mig_profile ?? "disabled"}
                  </Field>
                  <Field label="Topology domain">
                    {device.topology_domain_id ? (
                      <Link
                        to="/devices"
                        search={{ domain: device.topology_domain_id }}
                        className="hover:underline"
                      >
                        {device.topology_domain_id}
                        {device.topology_kind &&
                          ` · ${TOPOLOGY_KIND_LABEL[device.topology_kind]}`}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </Field>
                </FieldGrid>
              </CardContent>
            </Card>

            <Card className="lg:col-span-2">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Allocation</CardTitle>
              </CardHeader>
              <CardContent>
                {device.allocation ? (
                  <FieldGrid>
                    <Field label="Replica">{device.allocation.replica_id}</Field>
                    <Field label="Deployment">
                      {device.allocation.deployment_name} ·{" "}
                      {device.allocation.deployment_id}
                    </Field>
                    <Field label="Project">
                      {device.allocation.project_name} ·{" "}
                      {device.allocation.project_id}
                    </Field>
                    <Field label="Allocated at">
                      {formatAbsolute(device.allocation.allocated_at)}
                    </Field>
                  </FieldGrid>
                ) : (
                  <p className="font-mono text-2xs text-muted-foreground/60">
                    Idle — no replica currently holds this device.
                  </p>
                )}
              </CardContent>
            </Card>
          </div>
        )}
      </div>
    </div>
  )
}
