import {
  TableLoading,
  TableMessage,
} from "@/components/layout/table-states"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatRelative, formatError } from "@/lib/format"
import type { SupplyNode } from "@/modules/supply/types"
import { Link } from "@tanstack/react-router"
import { CordonState, NodeConditionLabel } from "./indicators"

interface NodesTableProps {
  nodes: SupplyNode[] | undefined
  isLoading: boolean
  error?: unknown
  showCluster?: boolean
  showPool?: boolean
  empty?: string
}

export function NodesTable({
  nodes,
  isLoading,
  error,
  showCluster = false,
  showPool = true,
  empty = "No nodes have joined yet.",
}: NodesTableProps) {
  const columns = 7 + (showCluster ? 1 : 0) + (showPool ? 1 : 0)

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Node</TableHead>
            {showCluster && <TableHead>Cluster</TableHead>}
            {showPool && <TableHead>Pool</TableHead>}
            <TableHead>Condition</TableHead>
            <TableHead>Schedulability</TableHead>
            <TableHead className="text-right">Devices healthy</TableHead>
            <TableHead className="text-right">Allocated</TableHead>
            <TableHead>Topology</TableHead>
            <TableHead>Driver</TableHead>
            <TableHead className="text-right">Last seen</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {isLoading && <TableLoading columns={columns + 2} />}

          {!!error && (
            <TableMessage columns={columns + 2} tone="danger">
              {formatError(error)}
            </TableMessage>
          )}

          {nodes?.map((node) => (
            <TableRow key={node.id} className="hover:bg-accent/40">
              <TableCell>
                <Link
                  to="/nodes/$nodeId"
                  params={{ nodeId: node.id }}
                  className="block font-mono text-xs hover:underline"
                >
                  {node.name}
                  <span className="ml-2 text-2xs text-muted-foreground/50">
                    {node.instance_type}
                  </span>
                </Link>
              </TableCell>
              {showCluster && (
                <TableCell>
                  <Link
                    to="/clusters/$clusterId"
                    params={{ clusterId: node.cluster_id }}
                    className="font-mono text-2xs text-muted-foreground hover:underline"
                  >
                    {node.cluster_name}
                  </Link>
                </TableCell>
              )}
              {showPool && (
                <TableCell>
                  {node.pool_id ? (
                    <Link
                      to="/pools/$poolId"
                      params={{ poolId: node.pool_id }}
                      className="font-mono text-2xs text-muted-foreground hover:underline"
                    >
                      {node.pool_name}
                    </Link>
                  ) : (
                    <span className="font-mono text-2xs text-muted-foreground/50">
                      unpooled
                    </span>
                  )}
                </TableCell>
              )}
              <TableCell>
                <NodeConditionLabel condition={node.condition} />
              </TableCell>
              <TableCell>
                <CordonState cordoned={node.cordoned} />
              </TableCell>
              <TableCell className="text-right font-mono text-xs">
                {node.devices_healthy}/{node.device_count}
              </TableCell>
              <TableCell className="text-right font-mono text-xs">
                {node.devices_allocated}
              </TableCell>
              <TableCell className="font-mono text-2xs text-muted-foreground/70">
                {node.topology_domain_ids.join(", ") || "—"}
              </TableCell>
              <TableCell className="font-mono text-2xs text-muted-foreground">
                {node.driver_version ?? "—"}
              </TableCell>
              <TableCell className="text-right font-mono text-2xs text-muted-foreground/70">
                {formatRelative(node.last_seen_at)}
              </TableCell>
            </TableRow>
          ))}

          {nodes && nodes.length === 0 && !isLoading && (
            <TableMessage columns={columns + 2}>{empty}</TableMessage>
          )}
        </TableBody>
      </Table>
    </div>
  )
}
