import { DevicesTable as SupplyDevicesTable } from "@/components/supply/devices-table"
import { useClusterDevices } from "@/modules/clusters/queries"

export function DevicesTable({ clusterId }: { clusterId: string }) {
  const { data, isLoading, error } = useClusterDevices(clusterId)
  return (
    <SupplyDevicesTable
      devices={data?.items}
      isLoading={isLoading}
      error={error}
      empty="No devices reported."
    />
  )
}
