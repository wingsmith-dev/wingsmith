import {
  TableLoading,
  TableMessage,
} from "@/components/layout/table-states"
import { formatError } from "@/lib/format"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useClusterTopology } from "@/modules/clusters/queries"
import { TOPOLOGY_KIND_LABEL } from "@/modules/supply/display"
import { Link } from "@tanstack/react-router"

export function TopologyTable({ clusterId }: { clusterId: string }) {
  const { data, isLoading, error } = useClusterTopology(clusterId)

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Domain</TableHead>
            <TableHead>Fabric</TableHead>
            <TableHead>Interconnect</TableHead>
            <TableHead>Node</TableHead>
            <TableHead className="text-right">Devices healthy</TableHead>
            <TableHead className="text-right">Allocated</TableHead>
            <TableHead className="text-right">Free</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {isLoading && <TableLoading columns={7} />}
          {error && (
            <TableMessage columns={7} tone="danger">
              {formatError(error)}
            </TableMessage>
          )}
          {data?.items.map((domain) => (
            <TableRow key={domain.id}>
              <TableCell>
                <Link
                  to="/devices"
                  search={{ domain: domain.id }}
                  className="font-mono text-xs hover:underline"
                >
                  {domain.id}
                </Link>
              </TableCell>
              <TableCell className="font-mono text-2xs">
                {TOPOLOGY_KIND_LABEL[domain.kind]}
              </TableCell>
              <TableCell className="font-mono text-2xs text-muted-foreground">
                {domain.interconnect}
                {domain.bandwidth_gbps && ` · ${domain.bandwidth_gbps} GB/s`}
              </TableCell>
              <TableCell>
                <Link
                  to="/nodes/$nodeId"
                  params={{ nodeId: domain.node_id }}
                  className="font-mono text-2xs text-muted-foreground hover:underline"
                >
                  {domain.node_name}
                </Link>
              </TableCell>
              <TableCell className="text-right font-mono text-xs">
                {domain.devices_healthy}/{domain.device_count}
              </TableCell>
              <TableCell className="text-right font-mono text-xs">
                {domain.devices_allocated}
              </TableCell>
              <TableCell className="text-right font-mono text-xs">
                {domain.devices_healthy - domain.devices_allocated}
              </TableCell>
            </TableRow>
          ))}
          {data && data.items.length === 0 && (
            <TableMessage columns={7}>
              No topology domains reported.
            </TableMessage>
          )}
        </TableBody>
      </Table>
    </div>
  )
}
