import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { StatusDot } from "@/components/layout/status-dot"
import { cn } from "@/lib/utils"
import { assignmentsOf } from "@/modules/board/build"
import {
  brickOfHolder,
  fitFromDevice,
  fitOnTarget,
  heldBy,
} from "@/modules/board/sandbox"
import { useBoardStore } from "@/modules/board/store"
import type {
  Board,
  BoardCluster,
  BoardCounts,
  BoardDevice,
  BoardFabric,
  BoardRegion,
  Brick,
  Fit,
} from "@/modules/board/types"
import { CLUSTER_PHASE_TONE } from "@/modules/clusters/display"
import { topologyKindLabel } from "@/modules/supply/display"
import { IconChevronRight, IconWorld } from "@tabler/icons-react"
import { useMemo } from "react"
import { toast } from "sonner"
import { DeviceCell } from "./DeviceCell"
import { HATCH } from "./Legend"
import type { BoardMode } from "./route"

/** Devices are grouped in fours, the way a node's GPUs are read off a chassis. */
const GROUP = 4

/**
 * The board starts at the cluster, because that is the layer an operator works
 * at: a cluster is a thing you place into, and a region is a property it has.
 * Grouping by region is the zoom-out, offered rather than assumed — it earns its
 * space only once there are enough clusters for "which region" to be the
 * question.
 */
export function FleetBoard({
  board,
  isLoading,
  mode,
  groupRegions,
}: {
  board: Board | undefined
  isLoading: boolean
  mode: BoardMode
  groupRegions: boolean
}) {
  const collapsed = useBoardStore((s) => s.collapsed)
  const toggle = useBoardStore((s) => s.toggle)
  const collapseAll = useBoardStore((s) => s.collapseAll)
  const select = useBoardStore((s) => s.select)
  const selection = useBoardStore((s) => s.selection)
  const highlight = useBoardStore((s) => s.highlight)
  const armed = useBoardStore((s) => s.armed)
  const dragging = useBoardStore((s) => s.dragging)
  const drag = useBoardStore((s) => s.drag)
  const place = useBoardStore((s) => s.place)
  const relocate = useBoardStore((s) => s.relocate)

  const assignments = useMemo(
    () => (board ? assignmentsOf(board) : new Map()),
    [board]
  )

  /**
   * The brick in hand, from wherever it came. A tray brick armed by a click and
   * one held mid-drag are the same thing to every fit check below, which is what
   * makes click-to-place and drag-to-move one code path.
   */
  const inHand: Brick | null = useMemo(() => {
    if (!board) return null
    if (dragging?.kind === "brick") return dragging.brick
    if (dragging?.kind === "allocation") {
      return brickOfHolder(board, dragging.holder)
    }
    return mode === "sandbox" ? armed : null
  }, [board, dragging, armed, mode])

  /**
   * Where the brick in hand would fit, per fabric. Computed for the whole board
   * rather than for the fabric under the pointer: the answer a person wants
   * while holding a brick is "where can this go", and highlighting only what
   * they already found makes them hunt for it.
   */
  const fits = useMemo(() => {
    const found = new Map<string, Fit>()
    if (!board || !inHand) return found
    for (const region of board.regions) {
      for (const cluster of region.clusters) {
        for (const fabric of cluster.fabrics) {
          found.set(
            fabric.key,
            fitOnTarget(cluster, fabric, inHand, assignments)
          )
        }
      }
    }
    return found
  }, [board, inHand, assignments])

  if (isLoading && !board) return <BoardSkeleton />
  if (!board || board.regions.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-10 text-center font-mono text-2xs text-muted-foreground/60">
        No devices are reporting in this scope.
      </div>
    )
  }

  const commit = (fit: Fit, fallbackFabric: string) => {
    if (!inHand) return
    if (!fit.ok) {
      toast.error(fit.reason ?? "does not fit", {
        description: `${inHand.label} · ${inHand.device_count}× ${inHand.device_class}`,
      })
      return
    }
    if (dragging?.kind === "allocation") {
      relocate(dragging.allocation, dragging.holder, dragging.from, fit.devices)
      toast.success(`${inHand.label} moved`, {
        description: `${fit.devices.length} devices · ${fallbackFabric}`,
      })
    } else {
      place(inHand, fit.devices)
      toast.success(`${inHand.label} placed`, {
        description: `${fit.devices.length} devices · ${fallbackFabric}`,
      })
    }
    drag(null)
  }

  const onFabric = (cluster: BoardCluster, fabric: BoardFabric) => {
    if (!inHand) {
      select({ kind: "fabric", key: fabric.key })
      return
    }
    commit(
      fits.get(fabric.key) ?? fitOnTarget(cluster, fabric, inHand, assignments),
      fabric.label
    )
  }

  const onDevice = (fabric: BoardFabric, cell: BoardDevice) => {
    if (!inHand) {
      select({ kind: "device", id: cell.device.id })
      return
    }
    commit(
      fitFromDevice(board, cell.device.id, inHand, assignments),
      fabric.label
    )
  }

  const pickUp = (cell: BoardDevice) => {
    const holder = cell.holder
    if (!holder || mode !== "sandbox") return
    if (holder.origin === "reserved") return
    drag({
      kind: "allocation",
      allocation: holder.allocation,
      holder,
      from: heldBy(board, holder.allocation),
    })
  }

  const clusterKeys = board.regions.flatMap((region) =>
    region.clusters.map((cluster) => `cluster:${cluster.cluster.id}`)
  )

  const renderCluster = (cluster: BoardCluster) => (
    <ClusterCard
      key={cluster.cluster.id}
      cluster={cluster}
      collapsed={!!collapsed[`cluster:${cluster.cluster.id}`]}
      onToggle={() => toggle(`cluster:${cluster.cluster.id}`)}
      onSelect={() => select({ kind: "cluster", id: cluster.cluster.id })}
    >
      {cluster.fabrics.map((fabric) => {
        const fabricSelected =
          selection?.kind === "fabric" && selection.key === fabric.key
        return (
          <FabricRow
            key={fabric.key}
            fabric={fabric}
            fit={inHand ? fits.get(fabric.key) : undefined}
            selected={fabricSelected}
            onActivate={() => onFabric(cluster, fabric)}
          >
            {runs(fabric.devices).map((group) => (
              <span
                key={group[0].device.id}
                className="flex items-center gap-1"
              >
                {group.map((cell) => (
                  <DeviceCell
                    key={cell.device.id}
                    cell={cell}
                    selected={
                      selection?.kind === "device" &&
                      selection.id === cell.device.id
                    }
                    withinSelection={fabricSelected}
                    dimmed={!!highlight && cell.holder?.workload !== highlight}
                    candidate={
                      !!inHand &&
                      (fits.get(fabric.key)?.devices.includes(cell.device.id) ??
                        false)
                    }
                    onSelect={() => onDevice(fabric, cell)}
                    onPickUp={mode === "sandbox" ? pickUp : undefined}
                    onDropOn={
                      mode === "sandbox"
                        ? () => onDevice(fabric, cell)
                        : undefined
                    }
                  />
                ))}
              </span>
            ))}
          </FabricRow>
        )
      })}
    </ClusterCard>
  )

  return (
    <div className="min-w-0 space-y-4" onDragEnd={() => drag(null)}>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border bg-card px-4 py-3">
        <Tally counts={board.counts} />
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 font-mono text-2xs text-muted-foreground"
            onClick={() => collapseAll(clusterKeys, true)}
          >
            Collapse all
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 font-mono text-2xs text-muted-foreground"
            onClick={() => collapseAll(clusterKeys, false)}
          >
            Expand all
          </Button>
        </div>
      </div>

      {groupRegions
        ? board.regions.map((region) => (
            <RegionSection
              key={region.region}
              region={region}
              collapsed={!!collapsed[`region:${region.region}`]}
              onToggle={() => toggle(`region:${region.region}`)}
              onSelect={() => select({ kind: "region", region: region.region })}
            >
              {region.clusters.map(renderCluster)}
            </RegionSection>
          ))
        : board.regions.flatMap((region) => region.clusters).map(renderCluster)}
    </div>
  )
}

/**
 * The row broken into the spans that get drawn together: groups of four, and
 * never one that spans two nodes. A fabric over several machines is one fabric
 * and several chassis, and a four-square group straddling that seam reads as a
 * card group that does not exist.
 */
function runs(cells: BoardDevice[]): BoardDevice[][] {
  const out: BoardDevice[][] = []
  for (const cell of cells) {
    const last = out[out.length - 1]
    const same = last && last[0].device.node === cell.device.node
    if (same && last.length < GROUP) last.push(cell)
    else out.push([cell])
  }
  return out
}

function RegionSection({
  region,
  collapsed,
  onToggle,
  onSelect,
  children,
}: React.PropsWithChildren<{
  region: BoardRegion
  collapsed: boolean
  onToggle: () => void
  onSelect: () => void
}>) {
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2 px-1">
        <button
          type="button"
          data-keeps-selection
          onClick={onToggle}
          className="flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
          aria-label={collapsed ? "Expand region" : "Collapse region"}
        >
          <IconChevronRight
            className={cn(
              "size-3.5 transition-transform",
              !collapsed && "rotate-90"
            )}
          />
          <IconWorld className="size-3.5" />
        </button>
        <button
          type="button"
          data-keeps-selection
          onClick={onSelect}
          className="font-mono text-2xs font-semibold tracking-[0.15em] uppercase hover:underline"
        >
          {region.region}
        </button>
        <span className="font-mono text-2xs text-muted-foreground/50">
          {region.clusters.length}{" "}
          {region.clusters.length === 1 ? "cluster" : "clusters"}
        </span>
        <div className="ml-auto">
          <Tally counts={region.counts} compact />
        </div>
      </div>
      {!collapsed && <div className="space-y-3">{children}</div>}
    </section>
  )
}

function ClusterCard({
  cluster,
  collapsed,
  onToggle,
  onSelect,
  children,
}: React.PropsWithChildren<{
  cluster: BoardCluster
  collapsed: boolean
  onToggle: () => void
  onSelect: () => void
}>) {
  const { cluster: meta, counts } = cluster
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div className="flex items-center gap-3 px-3 py-2.5">
        <button
          type="button"
          data-keeps-selection
          onClick={onToggle}
          aria-label={collapsed ? "Expand cluster" : "Collapse cluster"}
          className="text-muted-foreground hover:text-foreground"
        >
          <IconChevronRight
            className={cn(
              "size-4 transition-transform",
              !collapsed && "rotate-90"
            )}
          />
        </button>
        <StatusDot tone={CLUSTER_PHASE_TONE[meta.phase] ?? "neutral"} />
        <button
          type="button"
          data-keeps-selection
          onClick={onSelect}
          className="flex min-w-0 flex-col items-start gap-0.5 text-left"
        >
          <span className="truncate font-mono text-xs font-semibold hover:underline">
            {meta.name}
          </span>
          <span className="truncate font-mono text-2xs text-muted-foreground/60">
            {[meta.region, meta.display_name].filter(Boolean).join(" · ")}
            {meta.reachable ? "" : " · unreachable"}
            {cluster.reservations.length > 0 && (
              <span className="text-primary">
                {" · "}
                {cluster.reservations.length} incoming
              </span>
            )}
          </span>
        </button>

        <div className="ml-auto flex items-center gap-4">
          <span className="font-mono text-2xs text-muted-foreground/60">
            largest block{" "}
            <span
              className={cn(
                "font-semibold",
                cluster.largest_block >= 8
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-foreground"
              )}
            >
              {cluster.largest_block}
            </span>
          </span>
          <UtilBar counts={counts} className="w-28" />
          <Tally counts={counts} compact />
        </div>
      </div>
      {!collapsed && (
        <div className="space-y-0.5 border-t bg-background/40 p-2">
          {cluster.fabrics.length === 0 ? (
            <p className="px-2 py-1 font-mono text-2xs text-muted-foreground/50">
              No devices reporting.{" "}
              {meta.sync.state === "never_connected"
                ? "This cluster has never connected."
                : `Sync is ${meta.sync.state}.`}
            </p>
          ) : (
            children
          )}
        </div>
      )}
    </div>
  )
}

function FabricRow({
  fabric,
  fit,
  selected,
  onActivate,
  children,
}: React.PropsWithChildren<{
  fabric: BoardFabric
  fit: Fit | undefined
  selected: boolean
  onActivate: () => void
}>) {
  const spec = [
    topologyKindLabel(fabric.kind),
    fabric.device_class,
    fabric.domain?.link_bandwidth_gbps
      ? `${fabric.domain.link_bandwidth_gbps} GB/s`
      : null,
    fabric.nodes.length > 1 ? `${fabric.nodes.length} nodes` : null,
  ]
    .filter(Boolean)
    .join(" · ")

  return (
    <div
      data-keeps-selection
      onClick={onActivate}
      onDragOver={(event) => {
        if (fit?.ok) event.preventDefault()
      }}
      onDrop={(event) => {
        event.preventDefault()
        onActivate()
      }}
      className={cn(
        "flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-2 rounded-md px-2 py-1.5 transition-colors",
        "hover:bg-accent/40",
        selected && "bg-accent/60",
        fit?.ok && "bg-emerald-500/5 ring-1 ring-emerald-500/40 ring-inset",
        fit && !fit.ok && "opacity-50"
      )}
    >
      <div className="min-w-0 basis-72 pr-4">
        <div className="truncate font-mono text-2xs">{fabric.label}</div>
        <div className="truncate font-mono text-2xs text-muted-foreground/50">
          {spec}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        {children}
      </div>

      <div className="ml-auto flex shrink-0 items-center gap-3 font-mono text-2xs">
        {fit && !fit.ok && (
          <span className="text-amber-600 dark:text-amber-400">
            {fit.reason}
          </span>
        )}
        <span className="text-muted-foreground/60">
          {fabric.counts.held}/{fabric.counts.total} held
        </span>
        {fabric.counts.reserved > 0 && (
          <span className="text-primary">
            {fabric.counts.reserved} reserved
          </span>
        )}
        <span
          className={cn(
            "w-10 text-right",
            fabric.counts.available > 0
              ? "text-emerald-600 dark:text-emerald-400"
              : "text-muted-foreground/40"
          )}
        >
          {fabric.counts.available} free
        </span>
      </div>
    </div>
  )
}

/**
 * The numerals, and the only place reserved capacity appears at all.
 *
 * `reserved` and `available` are shown only when something is actually owed,
 * which keeps the ordinary board unchanged and makes the pair mean something
 * when it does appear: "8 free · 6 reserved · 2 available" is the whole
 * explanation for a cluster that looks idle and refuses work, and printing
 * "available 8" beside "free 8" every other minute of the day would train
 * everybody to stop reading it.
 *
 * These numerals are the exact half of the answer. The board also paints cells
 * for a reservation, but only the count here is authoritative: a cluster can owe
 * more than it has free, in which case fewer cells are painted than this says.
 * When the two disagree, this is the one to believe.
 */
function Tally({
  counts,
  compact = false,
}: {
  counts: BoardCounts
  compact?: boolean
}) {
  const items: [string, number, string][] = [
    ["held", counts.held, "text-foreground"],
    ["free", counts.free, "text-emerald-600 dark:text-emerald-400"],
    ...(counts.reserved > 0
      ? ([
          ["reserved", counts.reserved, "text-primary"],
          [
            "available",
            counts.available,
            "text-emerald-600 dark:text-emerald-400",
          ],
        ] as [string, number, string][])
      : []),
    ["fault", counts.fault, "text-destructive"],
    ["cordoned", counts.cordoned, "text-amber-600 dark:text-amber-400"],
    ["total", counts.total, "text-muted-foreground"],
  ]
  return (
    <div className={cn("flex items-center", compact ? "gap-3" : "gap-6")}>
      {items.map(([label, value, tone]) => (
        <div key={label} className="flex items-baseline gap-1.5">
          <span
            className={cn(
              "font-mono tabular-nums",
              compact ? "text-2xs" : "text-sm font-semibold",
              value === 0 ? "text-muted-foreground/40" : tone
            )}
          >
            {value}
          </span>
          <span className="font-mono text-2xs tracking-wider text-muted-foreground/50 uppercase">
            {label}
          </span>
        </div>
      ))}
    </div>
  )
}

/**
 * The bar earns a reserved segment where a device cell does not.
 *
 * A cell is one accelerator and naming one would be a claim; a bar is a
 * proportion of a cluster and claims nothing about which end of it is spoken
 * for. It is striped rather than solid because it is a promise and not an
 * observation, and it is clamped to what is actually free so a cluster that owes
 * more than it holds cannot draw a bar past its own width.
 */
function UtilBar({
  counts,
  className,
}: {
  counts: BoardCounts
  className?: string
}) {
  const pct = (value: number) =>
    counts.total ? (value / counts.total) * 100 : 0
  return (
    <div
      className={cn(
        "flex h-1.5 overflow-hidden rounded-full bg-secondary",
        className
      )}
    >
      <span
        className="h-full bg-primary"
        style={{ width: `${pct(counts.held)}%` }}
      />
      <span
        className={cn("h-full bg-primary/25 text-primary/70", HATCH)}
        style={{ width: `${pct(Math.min(counts.reserved, counts.free))}%` }}
      />
      <span
        className="h-full bg-amber-500/70"
        style={{ width: `${pct(counts.cordoned)}%` }}
      />
      <span
        className="h-full bg-destructive"
        style={{ width: `${pct(counts.fault)}%` }}
      />
    </div>
  )
}

function BoardSkeleton() {
  return (
    <div className="space-y-4">
      {[0, 1].map((section) => (
        <div key={section} className="space-y-2">
          <Skeleton className="h-3 w-24" />
          <div className="space-y-2 rounded-lg border bg-card p-3">
            {[0, 1, 2].map((row) => (
              <div key={row} className="flex items-center gap-3">
                <Skeleton className="h-6 w-72" />
                <div className="flex gap-1">
                  {Array.from({ length: 8 }).map((_, cell) => (
                    <Skeleton key={cell} className="size-6 rounded-[3px]" />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
