import { DataTable } from "@/components/layout/data-table"
import { operationColumns } from "@/components/machinery/operation-columns"
import { useOperations } from "@/modules/machinery/queries"
import type { Device } from "@/modules/supply/types"

/**
 * History, below the observed detail. Resetting a device is an Operation with a
 * lifecycle, not a button that returned 200, and the record of who asked for
 * one outlives the device's current health.
 */
export function DeviceOperations({ device }: { device: Device }) {
  const list = useOperations({ target: device.id })

  return (
    <div>
      <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
        Operation history
      </h2>
      <DataTable
        columns={operationColumns()}
        list={list}
        rowKey={(operation) => operation.id}
        empty="Nothing has been asked of this device."
      />
    </div>
  )
}
