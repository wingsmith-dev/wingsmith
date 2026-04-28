import { DataTable } from "@/components/layout/data-table"
import { deviceColumns } from "@/components/supply/columns"
import { Badge } from "@/components/ui/badge"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { gibibytes, topologyKindLabel } from "@/modules/supply/display"
import { useDeviceClassLabel } from "@/modules/supply/queries"
import type { Device } from "@/modules/supply/types"
import { IconChevronRight } from "@tabler/icons-react"
import { Link } from "@tanstack/react-router"
import { useState } from "react"

const GANG_SIZE = 8

export type GroupMode = "class" | "cluster" | "topology"

/**
 * Devices bucketed for reading, three ways. Grouping happens over what has been
 * loaded, so a group that looks incomplete may just be a page boundary — the
 * page's "load more" says as much.
 */
export function GroupedDevices({
  devices,
  by = "topology",
}: {
  devices: Device[]
  by?: GroupMode
}) {
  if (by === "class") return <ByModel devices={devices} />
  return <ByBucket devices={devices} by={by} />
}

/** By model — the class rollup, each row a class over the clusters that hold it. */
function ByModel({ devices }: { devices: Device[] }) {
  const byClass = new Map<string, Device[]>()
  for (const device of devices) {
    const bucket = byClass.get(device.device_class)
    if (bucket) bucket.push(device)
    else byClass.set(device.device_class, [device])
  }
  const groups = [...byClass.entries()].sort(
    (a, b) => b[1].length - a[1].length
  )

  return (
    <div className="space-y-3">
      {groups.map(([name, group]) => (
        <ModelGroup key={name} name={name} devices={group} />
      ))}
    </div>
  )
}

interface ClusterRollup {
  cluster: string
  cluster_name: string
  total: number
  allocated: number
  free: number
  fault: number
}

function rollup(devices: Device[]) {
  const total = devices.length
  const free = devices.filter((d) => d.allocatable).length
  const fault = devices.filter((d) => d.health !== "healthy").length
  const allocated = Math.max(0, total - free - fault)
  return { total, free, fault, allocated }
}

function ModelGroup({ name, devices }: { name: string; devices: Device[] }) {
  const [open, setOpen] = useState(false)
  const label = useDeviceClassLabel()(name)
  const { total, free, fault, allocated } = rollup(devices)

  const byCluster = new Map<string, Device[]>()
  for (const device of devices) {
    const bucket = byCluster.get(device.cluster)
    if (bucket) bucket.push(device)
    else byCluster.set(device.cluster, [device])
  }
  const clusters: ClusterRollup[] = [...byCluster.values()].map((group) => ({
    cluster: group[0].cluster,
    cluster_name: group[0].cluster_name,
    ...rollup(group),
  }))

  const allocPct = total ? (allocated / total) * 100 : 0
  const faultPct = total ? (fault / total) * 100 : 0

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="overflow-hidden rounded-lg border bg-card"
    >
      <CollapsibleTrigger className="flex w-full items-center gap-4 px-4 py-3.5 text-left hover:bg-accent/40">
        <IconChevronRight
          className={cn(
            "size-4 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90"
          )}
        />
        <div className="flex min-w-[11rem] flex-col gap-0.5">
          <span className="text-sm font-semibold">{label}</span>
          <span className="font-mono text-2xs text-muted-foreground/60">
            {specLine(devices[0])}
          </span>
        </div>

        <div className="flex min-w-[6rem] flex-1 flex-col gap-1.5">
          <div className="flex justify-between font-mono text-2xs text-muted-foreground">
            <span>
              {allocated} allocated / {total}
            </span>
            <span>
              {clusters.length} {clusters.length === 1 ? "cluster" : "clusters"}
            </span>
          </div>
          <div className="flex h-2 overflow-hidden rounded-full bg-secondary">
            <span
              className="h-full bg-primary"
              style={{ width: `${allocPct}%` }}
            />
            <span
              className="h-full bg-destructive"
              style={{ width: `${faultPct}%` }}
            />
          </div>
        </div>

        <div className="flex shrink-0 gap-5">
          <Count value={free} label="free" tone="free" />
          <Count value={fault} label="fault" tone="fault" />
          <Count value={total} label="total" />
        </div>
      </CollapsibleTrigger>

      <CollapsibleContent className="border-t px-4 pb-1">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Cluster</TableHead>
              <TableHead className="text-right">Total</TableHead>
              <TableHead className="text-right">Allocated</TableHead>
              <TableHead className="text-right">Free</TableHead>
              <TableHead className="text-right">Fault</TableHead>
              <TableHead className="w-40">Allocation</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {clusters.map((row) => (
              <ClusterRow key={row.cluster} row={row} />
            ))}
          </TableBody>
        </Table>
      </CollapsibleContent>
    </Collapsible>
  )
}

function ClusterRow({ row }: { row: ClusterRollup }) {
  // TODO-API "Allocation" is allocated/total from device state — real GPU
  // utilisation is telemetry and does not flow through the plane.
  const pct = row.total ? Math.round((row.allocated / row.total) * 100) : 0
  const hot = pct >= 90
  return (
    <TableRow>
      <TableCell>
        <Link
          to="/clusters/$clusterId"
          params={{ clusterId: row.cluster }}
          className="inline-flex items-center gap-1 font-mono text-xs hover:underline"
        >
          {row.cluster_name}
        </Link>
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {row.total}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {row.allocated}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {row.free > 0 ? (
          <span className="text-emerald-600 dark:text-emerald-400">
            {row.free}
          </span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {row.fault > 0 ? (
          <span className="text-amber-600 dark:text-amber-400">
            {row.fault}
          </span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-2">
          <div className="h-1.5 min-w-14 flex-1 overflow-hidden rounded-full bg-secondary">
            <div
              className={cn(
                "h-full rounded-full",
                hot ? "bg-destructive" : "bg-primary"
              )}
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className="w-9 text-right font-mono text-2xs text-muted-foreground tabular-nums">
            {pct}%
          </span>
        </div>
      </TableCell>
    </TableRow>
  )
}

function Count({
  value,
  label,
  tone,
}: {
  value: number
  label: string
  tone?: "free" | "fault"
}) {
  const zero = value === 0
  const color = zero
    ? "text-muted-foreground/50 font-normal"
    : tone === "free"
      ? "text-emerald-600 dark:text-emerald-400"
      : tone === "fault"
        ? "text-destructive"
        : ""
  return (
    <div className="flex flex-col items-end gap-0.5">
      <span
        className={cn("font-mono text-sm font-semibold tabular-nums", color)}
      >
        {value}
      </span>
      <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </span>
    </div>
  )
}

/**
 * The one-line spec under a class name. Memory and vendor are observed on the
 * device; fabric generation and board power are not carried in device state.
 */
function specLine(device: Device | undefined): string {
  if (!device) return ""
  // TODO-API fabric link (NVLink/PCIe gen) and board power (W) are not in
  // DeviceSchema — the reference shows them, device state does not carry them.
  return [
    gibibytes(device.memory_mib),
    device.vendor,
    device.compute_capability,
  ]
    .filter(Boolean)
    .join(" · ")
}

/** By cluster or by topology domain — devices under the fabric they share. */
function ByBucket({
  devices,
  by,
}: {
  devices: Device[]
  by: "cluster" | "topology"
}) {
  const byKey = new Map<string, Device[]>()
  for (const device of devices) {
    const key =
      by === "cluster"
        ? device.cluster_name
        : (device.topology?.identifier ?? "unassigned")
    const bucket = byKey.get(key)
    if (bucket) bucket.push(device)
    else byKey.set(key, [device])
  }

  return (
    <div className="space-y-5">
      {[...byKey.entries()].map(([identifier, group]) => {
        const healthy = group.filter((d) => d.health === "healthy").length
        const free = group.filter((d) => d.allocatable).length
        const kind = by === "topology" ? group[0]?.topology?.kind : undefined

        return (
          <section key={identifier} className="space-y-2">
            <div className="flex flex-wrap items-baseline gap-3">
              <span className="font-mono text-xs">{identifier}</span>
              {kind && (
                <span className="font-mono text-2xs text-muted-foreground/60">
                  {topologyKindLabel(kind)}
                </span>
              )}
              <span className="font-mono text-2xs text-muted-foreground/60">
                {healthy}/{group.length} healthy · {free} allocatable
              </span>
              {by === "topology" && free >= GANG_SIZE && (
                <Badge
                  variant="outline"
                  className="border-emerald-500/40 bg-emerald-500/10 font-mono text-2xs text-emerald-600 dark:text-emerald-400"
                >
                  {GANG_SIZE}-way free
                </Badge>
              )}
            </div>
            <DataTable
              columns={deviceColumns({ showTopology: by === "cluster" })}
              list={{
                items: group,
                loaded: group.length,
                isLoading: false,
                error: null,
                hasNextPage: false,
                isFetchingNextPage: false,
                fetchNextPage: () => {},
              }}
              rowKey={(device) => device.id}
              empty="No devices."
            />
          </section>
        )
      })}
    </div>
  )
}
