import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { DeviceHealthLabel } from "@/components/supply/indicators"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError, formatRelative } from "@/lib/format"
import { gibibytes, topologyKindLabel } from "@/modules/supply/display"
import { useDevice } from "@/modules/supply/queries"
import { useAllocations } from "@/modules/machinery/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"

export function DeviceDetailPage() {
  const { deviceId } = useParams({ from: "/_app/devices/$deviceId" })
  const { data: device, isLoading, error } = useDevice(deviceId)
  const allocations = useAllocations({ device: device?.id })

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Device" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load device")}
        </div>
      </div>
    )
  }

  const holder = allocations.items?.find((a) => a.live)

  return (
    <div className="flex flex-col">
      <PageHeader
        title={device?.device_uuid ?? "Device"}
        description={device ? `${device.id} · ${device.device_class}` : undefined}
        actions={
          <div className="flex gap-2">
            {device && (
              <NewOperationDialog
                defaultType="device.reset"
                defaultTarget={device.id}
                trigger={
                  <Button size="sm" variant="outline">
                    Reset
                  </Button>
                }
              />
            )}
            <Button asChild variant="outline" size="sm">
              <Link to="/devices">
                <IconArrowLeft className="size-4" />
                All devices
              </Link>
            </Button>
          </div>
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
                  <Field label="Reason">
                    <Dash>{device.health_reason}</Dash>
                  </Field>
                  <Field label="ECC">
                    {device.ecc_enabled ? "enabled" : "disabled"}
                  </Field>
                  <Field label="ECC volatile">{device.ecc_volatile_errors}</Field>
                  <Field label="ECC aggregate">{device.ecc_aggregate_errors}</Field>
                  <Field label="Pending retirement">
                    {device.ecc_pending_retirement ? "yes" : "no"}
                  </Field>
                  <Field label="Throttle reasons">
                    {device.throttle_reasons.length
                      ? device.throttle_reasons.join(", ")
                      : "none"}
                  </Field>
                  <Field label="Allocatable">
                    {device.allocatable ? "yes" : "no"}
                  </Field>
                  <Field label="Observed">{formatRelative(device.observed_at)}</Field>
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
                      params={{ clusterId: device.cluster }}
                      className="hover:underline"
                    >
                      {device.cluster_name}
                    </Link>
                  </Field>
                  <Field label="Node">
                    <Link
                      to="/nodes/$nodeId"
                      params={{ nodeId: device.node }}
                      className="hover:underline"
                    >
                      {device.node_name}
                    </Link>
                  </Field>
                  <Field label="Index">#{device.device_index}</Field>
                  <Field label="Memory">{gibibytes(device.memory_mib)}</Field>
                  <Field label="Vendor / model">
                    {device.vendor} · <Dash>{device.model_name}</Dash>
                  </Field>
                  <Field label="PCI address">
                    <Dash>{device.pci_address}</Dash>
                  </Field>
                  <Field label="Compute capability">
                    <Dash>{device.compute_capability}</Dash>
                  </Field>
                  <Field label="MIG">
                    {device.mig_enabled ? (device.mig_profile ?? "enabled") : "disabled"}
                  </Field>
                  <Field label="Topology domain" className="sm:col-span-2">
                    {device.topology ? (
                      <Link
                        to="/devices"
                        search={{ domain: device.topology.id }}
                        className="hover:underline"
                      >
                        {device.topology.identifier} ·{" "}
                        {topologyKindLabel(device.topology.kind)}
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
                {holder ? (
                  <FieldGrid>
                    <Field label="Allocation">
                      <Link
                        to="/allocations"
                        search={{ device: device.id }}
                        className="hover:underline"
                      >
                        {holder.id}
                      </Link>
                    </Field>
                    <Field label="Workload">{holder.workload}</Field>
                    <Field label="Project">{holder.project_slug}</Field>
                    <Field label="Acquired">{formatRelative(holder.acquired_at)}</Field>
                  </FieldGrid>
                ) : (
                  <p className="font-mono text-2xs text-muted-foreground/60">
                    {allocations.isLoading
                      ? "Loading…"
                      : "No live allocation holds this device."}
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
