import {
  TableLoading,
  TableMessage,
} from "@/components/layout/table-states"
import { formatError } from "@/lib/format"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { Device } from "@/modules/supply/types"
import { Link } from "@tanstack/react-router"
import {
  AllocationCell,
  DeviceHealthLabel,
  EccCell,
  TopologyBadge,
} from "./indicators"

interface DevicesTableProps {
  devices: Device[] | undefined
  isLoading: boolean
  error?: unknown
  showNode?: boolean
  showTopology?: boolean
  empty?: string
}

export function DevicesTable({
  devices,
  isLoading,
  error,
  showNode = true,
  showTopology = true,
  empty = "No devices reported.",
}: DevicesTableProps) {
  const columns = 8 + (showNode ? 1 : 0) + (showTopology ? 1 : 0)

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Device</TableHead>
            {showNode && <TableHead>Node</TableHead>}
            <TableHead>Class</TableHead>
            <TableHead className="text-right">Memory</TableHead>
            <TableHead>Health</TableHead>
            <TableHead className="text-right">ECC (vol/agg)</TableHead>
            <TableHead>MIG</TableHead>
            {showTopology && <TableHead>Topology domain</TableHead>}
            <TableHead>Allocation</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {isLoading && <TableLoading columns={columns} rows={6} />}

          {!!error && (
            <TableMessage columns={columns} tone="danger">
              {formatError(error)}
            </TableMessage>
          )}

          {devices?.map((device) => (
            <TableRow key={device.id} className="hover:bg-accent/40">
              <TableCell>
                <Link
                  to="/devices/$deviceId"
                  params={{ deviceId: device.id }}
                  className="block font-mono text-xs hover:underline"
                >
                  {device.uuid}
                  <span className="ml-2 text-muted-foreground/50">
                    #{device.index}
                  </span>
                </Link>
              </TableCell>
              {showNode && (
                <TableCell>
                  <Link
                    to="/nodes/$nodeId"
                    params={{ nodeId: device.node_id }}
                    className="font-mono text-2xs text-muted-foreground hover:underline"
                  >
                    {device.node_name}
                  </Link>
                </TableCell>
              )}
              <TableCell>
                <Badge variant="secondary" className="font-mono text-2xs">
                  {device.device_class}
                </Badge>
              </TableCell>
              <TableCell className="text-right font-mono text-2xs text-muted-foreground">
                {device.memory_used_gb}/{device.memory_gb} GB
              </TableCell>
              <TableCell>
                <DeviceHealthLabel device={device} />
              </TableCell>
              <TableCell className="text-right">
                <EccCell device={device} />
              </TableCell>
              <TableCell className="font-mono text-2xs text-muted-foreground">
                {device.mig_profile ?? "—"}
              </TableCell>
              {showTopology && (
                <TableCell>
                  <TopologyBadge
                    domainId={device.topology_domain_id}
                    kind={device.topology_kind}
                  />
                </TableCell>
              )}
              <TableCell>
                <AllocationCell allocation={device.allocation} />
              </TableCell>
            </TableRow>
          ))}

          {devices && devices.length === 0 && !isLoading && (
            <TableMessage columns={columns}>{empty}</TableMessage>
          )}
        </TableBody>
      </Table>
    </div>
  )
}
