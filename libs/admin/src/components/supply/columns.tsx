import type { Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { ResourceCell } from "@/components/layout/resource-cell"
import { Badge } from "@/components/ui/badge"
import { formatRelative } from "@/lib/format"
import { gibibytes, topologyKindLabel, unhealthyCount } from "@/modules/supply/display"
import type { Device, SupplyNode, TopologyDomain } from "@/modules/supply/types"
import { Link } from "@tanstack/react-router"
import {
  AllocatableCell,
  CordonState,
  DeviceHealthLabel,
  EccCell,
  NodeConditionLabel,
} from "./indicators"

export function nodeColumns(options: { showCluster?: boolean; showPool?: boolean } = {}) {
  const { showCluster = true, showPool = true } = options
  const columns: Column<SupplyNode>[] = [
    {
      key: "name",
      header: "Node",
      cell: (node) => (
        <ResourceCell kind="node">
          <Link
            to="/nodes/$nodeId"
            params={{ nodeId: node.id }}
            className="block truncate font-mono text-xs hover:underline"
          >
            {node.name}
          </Link>
        </ResourceCell>
      ),
    },
  ]

  if (showCluster) {
    columns.push({
      key: "cluster",
      header: "Cluster",
      cell: (node) => (
        <Link
          to="/clusters/$clusterId"
          params={{ clusterId: node.cluster }}
          className="font-mono text-2xs text-muted-foreground hover:underline"
        >
          {node.cluster_name}
        </Link>
      ),
    })
  }

  if (showPool) {
    columns.push({
      key: "pool",
      header: "Pool",
      cell: (node) =>
        node.pool ? (
          <Link
            to="/pools/$poolId"
            params={{ poolId: node.pool }}
            className="font-mono text-2xs text-muted-foreground hover:underline"
          >
            {node.pool_name}
          </Link>
        ) : (
          <span className="font-mono text-2xs text-muted-foreground/50">unpooled</span>
        ),
    })
  }

  columns.push(
    {
      key: "condition",
      header: "Condition",
      cell: (node) => <NodeConditionLabel condition={node.condition} />,
    },
    {
      key: "cordoned",
      header: "Schedulability",
      cell: (node) => <CordonState cordoned={node.cordoned} />,
    },
    {
      key: "devices",
      header: "Devices",
      align: "right",
      cell: (node) => (
        <span className="font-mono text-xs">
          {node.devices.total - unhealthyCount(node.devices)}/{node.devices.total}
        </span>
      ),
    },
    {
      key: "unhealthy",
      header: "Unhealthy",
      align: "right",
      cell: (node) => {
        const bad = unhealthyCount(node.devices)
        return (
          <span
            className={
              bad > 0
                ? "font-mono text-xs text-amber-600 dark:text-amber-400"
                : "font-mono text-xs text-muted-foreground/50"
            }
          >
            {bad}
          </span>
        )
      },
    },
    {
      key: "driver",
      header: "Driver",
      secondary: true,
      cell: (node) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>{node.driver_version}</Dash>
        </span>
      ),
    },
    {
      key: "observed",
      header: "Observed",
      align: "right",
      cell: (node) => (
        <span className="font-mono text-2xs text-muted-foreground/70">
          {formatRelative(node.observed_at)}
        </span>
      ),
    }
  )

  return columns
}

export function deviceColumns(options: { showNode?: boolean; showTopology?: boolean } = {}) {
  const { showNode = true, showTopology = true } = options
  const columns: Column<Device>[] = [
    {
      key: "uuid",
      header: "Device",
      cell: (device) => (
        <ResourceCell kind="device">
          <Link
            to="/devices/$deviceId"
            params={{ deviceId: device.id }}
            className="block font-mono text-xs hover:underline"
          >
            {device.device_uuid}
            <span className="ml-2 text-muted-foreground/50">#{device.device_index}</span>
          </Link>
        </ResourceCell>
      ),
    },
  ]

  if (showNode) {
    columns.push({
      key: "node",
      header: "Node",
      cell: (device) => (
        <Link
          to="/nodes/$nodeId"
          params={{ nodeId: device.node }}
          className="font-mono text-2xs text-muted-foreground hover:underline"
        >
          {device.node_name}
        </Link>
      ),
    })
  }

  columns.push(
    {
      key: "class",
      header: "Class",
      cell: (device) => (
        <Badge variant="secondary" className="font-mono text-2xs">
          {device.device_class}
        </Badge>
      ),
    },
    {
      key: "memory",
      header: "Memory",
      align: "right",
      cell: (device) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {gibibytes(device.memory_mib)}
        </span>
      ),
    },
    {
      key: "health",
      header: "Health",
      cell: (device) => <DeviceHealthLabel device={device} />,
    },
    {
      key: "ecc",
      header: "ECC vol/agg",
      align: "right",
      secondary: true,
      cell: (device) => <EccCell device={device} />,
    },
    {
      key: "mig",
      header: "MIG",
      secondary: true,
      cell: (device) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>{device.mig_enabled ? (device.mig_profile ?? "enabled") : null}</Dash>
        </span>
      ),
    }
  )

  if (showTopology) {
    columns.push({
      key: "topology",
      header: "Topology domain",
      cell: (device) =>
        device.topology ? (
          <Link
            to="/devices"
            search={{ domain: device.topology.id }}
            className="inline-flex flex-col font-mono text-2xs hover:underline"
          >
            <span>{device.topology.identifier}</span>
            <span className="text-muted-foreground/50">
              {topologyKindLabel(device.topology.kind)}
            </span>
          </Link>
        ) : (
          <span className="font-mono text-2xs text-muted-foreground/40">—</span>
        ),
    })
  }

  columns.push({
    key: "allocatable",
    header: "Schedulable",
    cell: (device) => <AllocatableCell allocatable={device.allocatable} />,
  })

  return columns
}

export function topologyColumns(): Column<TopologyDomain>[] {
  return [
    {
      key: "identifier",
      header: "Domain",
      cell: (domain) => (
        <ResourceCell kind="topology-domain">
          <Link
            to="/devices"
            search={{ domain: domain.id }}
            className="block font-mono text-xs hover:underline"
          >
            {domain.identifier}
            <Id value={domain.id} className="ml-2" />
          </Link>
        </ResourceCell>
      ),
    },
    {
      key: "kind",
      header: "Fabric",
      cell: (domain) => (
        <span className="font-mono text-2xs">{topologyKindLabel(domain.kind)}</span>
      ),
    },
    {
      key: "link",
      header: "Link",
      cell: (domain) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>
            {domain.link_bandwidth_gbps
              ? `${domain.link_bandwidth_gbps} Gb/s${domain.link_generation ? ` · gen ${domain.link_generation}` : ""}`
              : null}
          </Dash>
        </span>
      ),
    },
    {
      key: "node",
      header: "Nodes",
      cell: (domain) =>
        domain.nodes.length === 1 ? (
          <Link
            to="/nodes/$nodeId"
            params={{ nodeId: domain.nodes[0] }}
            className="font-mono text-2xs text-muted-foreground hover:underline"
          >
            {domain.node_names[0]}
          </Link>
        ) : (
          <span className="font-mono text-2xs text-muted-foreground/50">
            {domain.nodes.length === 0 ? "none" : `${domain.nodes.length} nodes`}
          </span>
        ),
    },
    {
      key: "devices",
      header: "Devices",
      align: "right",
      cell: (domain) => (
        <span className="font-mono text-xs">{domain.device_count}</span>
      ),
    },
    {
      key: "spans",
      header: "Spans nodes",
      align: "right",
      secondary: true,
      cell: (domain) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {domain.spans_nodes ? "yes" : "no"}
        </span>
      ),
    },
  ]
}
