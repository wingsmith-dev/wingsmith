import { DataTable } from "@/components/layout/data-table"
import { deviceColumns } from "@/components/supply/columns"
import { useDevices } from "@/modules/supply/queries"

export function ClusterDevicesTable({ clusterId }: { clusterId: string }) {
  const list = useDevices({ cluster: clusterId })
  return (
    <DataTable
      columns={deviceColumns()}
      list={list}
      rowKey={(device) => device.id}
      empty="No devices reported."
    />
  )
}
