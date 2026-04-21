import type { Column } from "@/components/layout/data-table"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { ResourceCell } from "@/components/layout/resource-cell"
import { NodeConditionLabel } from "@/components/supply/indicators"
import { Card } from "@/components/ui/card"
import { formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { NODE_CONDITIONS, unhealthyCount } from "@/modules/supply/display"
import { useNodes } from "@/modules/supply/queries"
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
      <span className="font-mono text-2xs tracking-wider text-muted-foreground/70 uppercase">
        {label}
      </span>
      <span
        className={cn(
          "font-heading text-2xl leading-none font-semibold tabular-nums",
          tone === "warn" && "text-amber-600 dark:text-amber-400"
        )}
      >
        {value}
      </span>
      {note && (
        <span className="font-mono text-2xs text-amber-600 dark:text-amber-400">
          {note}
        </span>
      )}
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
            index >= total - bad
              ? "bg-destructive"
              : "bg-muted-foreground/25"
          )}
        />
      ))}
      {total === 0 && (
        <span className="font-mono text-2xs text-muted-foreground/40">—</span>
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
          <span className="font-mono text-2xs text-muted-foreground/50">
            unpooled
          </span>
        ),
    },
    {
      key: "gpu_model",
      header: "GPU model",
      cell: () => (
        // TODO-API node schema carries no device class; the GPU model per node
        // is not on the wire. Read from the pool's device_class once exposed.
        <span className="font-mono text-2xs text-muted-foreground/40">—</span>
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
      cell: (node) => {
        // TODO-API allocation count is not on the node schema; shown as
        // healthy-of-total until `/allocations` rollups reach this row.
        const usable = node.devices.total - unhealthyCount(node.devices)
        return (
          <span className="font-mono text-xs">
            {usable}/{node.devices.total}
          </span>
        )
      },
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
              ? "text-amber-600 dark:text-amber-400"
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
        <span className="font-mono text-2xs text-muted-foreground/70">
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
      new Set(
        (clusters ?? []).filter((c) => !c.reachable).map((c) => c.id)
      ),
    [clusters]
  )

  const { summary, driftDrivers } = useMemo(() => {
    const items = list.items ?? []
    // A node under an unreachable cluster is stale, not confirmably ready: the
    // agent stopped reporting, so `ready` is only the last thing it said.
    const stale = items.filter((node) => unreachable.has(node.cluster)).length
    const ready = items.filter(
      (node) => node.ready && !unreachable.has(node.cluster)
    ).length
    const faultedGpus = items.reduce(
      (sum, node) => sum + unhealthyCount(node.devices),
      0
    )

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
        total: items.length,
        ready,
        stale,
        driftNodes,
        faultedGpus,
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
          {/* TODO-API counts reflect loaded rows, not the fleet total; a
              server-side nodes summary would give true fleet-wide figures. */}
          <SummaryTile label="Nodes" value={summary.total} />
          <SummaryTile
            label="Ready"
            value={`${summary.ready}/${summary.total}`}
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
            value={summary.faultedGpus}
            tone={summary.faultedGpus > 0 ? "warn" : undefined}
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
