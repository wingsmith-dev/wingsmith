import type { Column } from "@/components/layout/data-table"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { ResourceCell } from "@/components/layout/resource-cell"
import {
  NodeConditionLabel,
  NodeKindBadge,
} from "@/components/supply/indicators"
import { Card } from "@/components/ui/card"
import { formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { NODE_CONDITIONS, unhealthyCount } from "@/modules/supply/display"
import { useNodes, useNodesSummary } from "@/modules/supply/queries"
import type { NodeDeviceRollup, SupplyNode } from "@/modules/supply/types"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"
import { useMemo } from "react"
import { GroupedNodes } from "./GroupedNodes"

const CORDONED = [
  { value: "true", label: "Cordoned" },
  { value: "false", label: "Schedulable" },
]

/** One headline number over the loaded rows. */
function SummaryTile({
  label,
  value,
  tone,
  note,
}: {
  label: string
  value: React.ReactNode
  tone?: "warn"
  note?: string
}) {
  return (
    <Card size="sm" className="gap-1.5 px-3">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        {label}
      </span>
      <span
        className={cn(
          "font-heading text-2xl leading-none font-semibold tabular-nums",
          tone === "warn" && "text-warning"
        )}
      >
        {value}
      </span>
      {note && <span className="font-mono text-2xs text-warning">{note}</span>}
    </Card>
  )
}

/**
 * A GPU-by-GPU strip for one node's device rollup. Fault cells come from the
 * observed health split; the allocated/reserved/free breakdown is not on the
 * node schema, so those never light here.
 */
function DeviceStrip({ devices }: { devices: NodeDeviceRollup }) {
  const bad = unhealthyCount(devices)
  const total = devices.total
  return (
    <span className="flex gap-0.5">
      {Array.from({ length: total }).map((_, index) => (
        <span
          key={index}
          className={cn(
            "h-3.5 w-[7px] rounded-[1px]",
            index >= total - bad ? "bg-destructive" : "bg-muted-foreground/25"
          )}
        />
      ))}
      {total === 0 && (
        <span className="font-mono text-2xs text-muted-foreground">—</span>
      )}
    </span>
  )
}

function StripLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1">
      {[
        { label: "allocated", className: "bg-primary" },
        { label: "free", className: "bg-emerald-500" },
        { label: "reserved", className: "bg-amber-500/55" },
        { label: "fault", className: "bg-destructive" },
      ].map((item) => (
        <span
          key={item.label}
          className="inline-flex items-center gap-1.5 font-mono text-2xs text-muted-foreground"
        >
          <span className={cn("h-3 w-[7px] rounded-[1px]", item.className)} />
          {item.label}
        </span>
      ))}
    </div>
  )
}

/**
 * The table shape from the reference: node, status, pool, GPU model, a device
 * strip, allocation, driver and uptime. Cluster is carried by the group header
 * so it is not repeated as a column. `driftDrivers` is the set of driver
 * versions that are not the fleet's most common one, highlighted in place.
 */
function nodeListColumns(
  driftDrivers: Set<string>,
  unreachable: Set<string>
): Column<SupplyNode>[] {
  return [
    {
      key: "name",
      header: "Node",
      cell: (node) => (
        <ResourceCell kind="node">
          <div className="flex min-w-0 items-center gap-2">
            <Link
              to="/nodes/$nodeId"
              params={{ nodeId: node.id }}
              className="truncate font-mono text-xs hover:underline"
            >
              {node.name}
            </Link>
            <NodeKindBadge devices={node.devices.total} />
          </div>
        </ResourceCell>
      ),
    },
    {
      key: "condition",
      header: "Status",
      cell: (node) => (
        <NodeConditionLabel
          condition={node.condition}
          stale={unreachable.has(node.cluster)}
        />
      ),
    },
    {
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
          <span className="font-mono text-2xs text-muted-foreground">
            unpooled
          </span>
        ),
    },
    {
      key: "gpu_model",
      header: "GPU model",
      cell: (node) =>
        node.devices.device_class ? (
          <span className="font-mono text-2xs">
            {node.devices.device_class}
          </span>
        ) : (
          <span className="font-mono text-2xs text-muted-foreground">—</span>
        ),
    },
    {
      key: "devices",
      header: "Devices",
      cell: (node) => <DeviceStrip devices={node.devices} />,
    },
    {
      key: "allocated",
      header: "Allocated",
      align: "right",
      cell: (node) =>
        node.devices.total > 0 ? (
          <span className="font-mono text-xs">
            {node.devices.allocated}/{node.devices.total}
          </span>
        ) : (
          <span className="font-mono text-xs text-muted-foreground">—</span>
        ),
    },
    {
      key: "driver",
      header: "Driver",
      secondary: true,
      cell: (node) => (
        <span
          className={cn(
            "font-mono text-2xs",
            node.driver_version && driftDrivers.has(node.driver_version)
              ? "text-warning"
              : "text-muted-foreground"
          )}
        >
          {node.driver_version ?? "—"}
        </span>
      ),
    },
    {
      key: "uptime",
      header: "Uptime",
      align: "right",
      cell: (node) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {node.joined_at ? formatRelative(node.joined_at) : "—"}
        </span>
      ),
    },
  ]
}

export function NodesPage() {
  const search = useSearch({ from: "/_app/nodes" })
  const navigate = useNavigate({ from: "/nodes" })
  const { data: clusters } = useClusterOptions()
  const { data: fleet } = useNodesSummary()

  const list = useNodes({
    cluster: search.cluster,
    pool: search.pool,
    condition: search.condition,
    cordoned:
      search.cordoned === undefined ? undefined : search.cordoned === "true",
  })

  const set = (key: "cluster" | "condition" | "cordoned") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  const unreachable = useMemo(
    () =>
      new Set((clusters ?? []).filter((c) => !c.reachable).map((c) => c.id)),
    [clusters]
  )

  const { summary, driftDrivers } = useMemo(() => {
    const items = list.items ?? []
    // A node under an unreachable cluster is stale, not confirmably ready: the
    // agent stopped reporting, so `ready` is only the last thing it said. This
    // stays over the loaded rows — the server summary counts readiness fleet-
    // wide but cannot know which clusters the console currently sees as stale.
    const stale = items.filter((node) => unreachable.has(node.cluster)).length

    const counts = new Map<string, number>()
    for (const node of items) {
      if (!node.driver_version) continue
      counts.set(
        node.driver_version,
        (counts.get(node.driver_version) ?? 0) + 1
      )
    }
    let modal: string | null = null
    let modalCount = -1
    for (const [version, count] of counts) {
      if (count > modalCount) {
        modal = version
        modalCount = count
      }
    }
    const drift = new Set(
      [...counts.keys()].filter((version) => version !== modal)
    )
    const driftNodes = items.filter(
      (node) => node.driver_version && drift.has(node.driver_version)
    ).length

    return {
      summary: {
        stale,
        driftNodes,
      },
      driftDrivers: drift,
    }
  }, [list.items, unreachable])

  const columns = useMemo(
    () => nodeListColumns(driftDrivers, unreachable),
    [driftDrivers, unreachable]
  )

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Nodes"
        description="observed · membership, conditions, cordon state"
      />

      <div className="space-y-4 p-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <SummaryTile label="Nodes" value={fleet?.total ?? "—"} />
          <SummaryTile
            label="Ready"
            value={fleet ? `${fleet.ready}/${fleet.total}` : "—"}
            tone={summary.stale > 0 ? "warn" : undefined}
            note={
              summary.stale > 0
                ? `${summary.stale} stale · cluster unreachable`
                : undefined
            }
          />
          <SummaryTile
            label="Driver drift"
            value={summary.driftNodes}
            tone={summary.driftNodes > 0 ? "warn" : undefined}
          />
          <SummaryTile
            label="Faulted GPUs"
            value={fleet?.faulted_devices ?? "—"}
            tone={fleet && fleet.faulted_devices > 0 ? "warn" : undefined}
          />
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <FilterSelect
              label="Cluster"
              value={search.cluster ?? ALL}
              onChange={set("cluster")}
              options={(clusters ?? []).map((cluster) => ({
                value: cluster.id,
                label: clusterLabel(cluster),
              }))}
            />
            <FilterSelect
              label="Condition"
              value={search.condition ?? ALL}
              onChange={set("condition")}
              options={NODE_CONDITIONS.map((c) => ({ value: c, label: c }))}
            />
            <FilterSelect
              label="Cordon"
              value={search.cordoned ?? ALL}
              onChange={set("cordoned")}
              options={CORDONED}
            />
          </div>
          <StripLegend />
        </div>

        <GroupedNodes
          columns={columns}
          list={list}
          empty="No nodes match these filters."
        />
      </div>
    </div>
  )
}
