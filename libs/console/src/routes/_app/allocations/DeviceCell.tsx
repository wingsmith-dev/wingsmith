import { cn } from "@/lib/utils"
import { brickColor } from "@/modules/board/palette"
import type { BoardDevice } from "@/modules/board/types"
import { gibibytes } from "@/modules/supply/display"

/**
 * The stripe that means "this device is not yours to place on". Faulted and
 * cordoned share the texture and differ in colour, because they are the same
 * fact for a placement decision and different facts for the person fixing it.
 */
const HATCH =
  "bg-[repeating-linear-gradient(45deg,currentColor_0px,currentColor_1px,transparent_1px,transparent_4px)]"

export interface CellProps {
  cell: BoardDevice
  selected: boolean
  /** Something else is highlighted, so this cell steps back. */
  dimmed: boolean
  /** A brick is in hand and this cell is one it could take. */
  candidate: boolean
  /**
   * The fabric this cell sits in is what the inspector is pointed at. Marked on
   * the cells and not only on the row, because a fabric is only meaningful as
   * the devices that share it — "these act as one" is the fact being selected.
   */
  withinSelection: boolean
  onSelect: (cell: BoardDevice) => void
  /** Double click, on a cell something holds: open the whole story of it. */
  onInspect?: (cell: BoardDevice) => void
  onPickUp?: (cell: BoardDevice) => void
  onDropOn?: (cell: BoardDevice) => void
}

/**
 * One device, and the slices inside it.
 *
 * A partitioned device is drawn as its bands rather than as a device, because
 * that is what it is once MIG is on: the parent holds nothing and is not
 * schedulable, and painting it as one free square would offer capacity that
 * cannot be taken. The bands say "split" at a glance; the inspector says into
 * what.
 */
export function DeviceCell(props: CellProps) {
  const {
    cell,
    selected,
    dimmed,
    candidate,
    withinSelection,
    onSelect,
    onInspect,
    onPickUp,
    onDropOn,
  } = props
  const held = cell.holder
  const partitioned = cell.slices.length > 0
  /**
   * A reservation is not yours to move. It has no allocation the plane would
   * recognise — the id on the holder is one this board invented so the cells
   * could be grouped — so dragging one would put a fabricated allocation into
   * the plan and ask the sandbox to relocate something that was never bound.
   */
  const movable = held && held.origin !== "reserved" ? held : null

  return (
    <button
      type="button"
      data-keeps-selection
      title={
        held
          ? `${describe(cell)}\ndouble click for what is running here`
          : describe(cell)
      }
      aria-label={describe(cell)}
      draggable={!!movable && !!onPickUp}
      onDragStart={(event) => {
        if (!movable || !onPickUp) return
        event.dataTransfer.effectAllowed = "move"
        event.dataTransfer.setData("text/plain", movable.allocation)
        onPickUp(cell)
      }}
      onDragOver={(event) => {
        if (onDropOn && candidate) event.preventDefault()
      }}
      onDrop={(event) => {
        if (!onDropOn) return
        event.preventDefault()
        event.stopPropagation()
        onDropOn(cell)
      }}
      onClick={(event) => {
        event.stopPropagation()
        onSelect(cell)
      }}
      onDoubleClick={(event) => {
        if (!onInspect) return
        event.stopPropagation()
        onInspect(cell)
      }}
      className={cn(
        "group/cell relative size-6 shrink-0 overflow-hidden rounded-[3px] transition-all",
        "focus-visible:ring-2 focus-visible:ring-primary focus-visible:outline-none",
        dimmed && "opacity-20",
        candidate &&
          "ring-1 ring-emerald-400/70 ring-offset-1 ring-offset-background",
        withinSelection &&
          "ring-1 ring-primary/50 ring-offset-1 ring-offset-background",
        selected && "ring-2 ring-primary ring-offset-1 ring-offset-background",
        movable && "cursor-grab active:cursor-grabbing"
      )}
    >
      {partitioned ? (
        <span className="flex size-full flex-col gap-px p-px">
          {cell.slices.map((slice) => (
            <span
              key={slice.device.id}
              className={cn("min-h-0 flex-1 rounded-[1px]", face(slice))}
            />
          ))}
        </span>
      ) : (
        <span className={cn("block size-full", face(cell))} />
      )}
    </button>
  )
}

/** The fill for one leaf — a whole device, or one slice of one. */
function face(cell: BoardDevice): string {
  if (cell.device.health !== "healthy") {
    return cn(
      "bg-destructive/20 text-destructive ring-1 ring-destructive/40 ring-inset",
      HATCH
    )
  }
  if (cell.cordoned) {
    return cn(
      "bg-muted text-muted-foreground ring-1 ring-border ring-inset",
      HATCH
    )
  }
  if (!cell.holder) {
    if (!cell.device.allocatable) {
      return "bg-muted/60 ring-1 ring-border ring-inset"
    }
    return "bg-muted/30 ring-1 ring-dashed ring-border ring-inset hover:bg-emerald-500/15 hover:ring-emerald-500/50"
  }
  const colour = brickColor(cell.holder.workload)
  /**
   * Reserved wears the workload's own colour, because the fact worth seeing is
   * "*this* workload is arriving *here*" and the colour is what says which
   * workload. What separates it from a bound cell is weight: a washed-out fill
   * over the free cell's own dashed ring, so it reads as an outline being
   * filled in rather than as something already there. It cannot be mistaken for
   * free (free carries no colour), for proposed (solid fill, dashed outline
   * inset from the edge), or for cordoned and faulted (both hatched).
   */
  if (cell.holder.origin === "reserved") {
    return cn(colour.ghost, "ring-dashed ring-1 ring-inset", colour.edge)
  }
  return cn(
    colour.fill,
    cell.holder.origin === "live"
      ? "ring-1 ring-black/10 ring-inset dark:ring-white/10"
      : "outline-2 outline-offset-[-3px] outline-background outline-dashed"
  )
}

/** The hover text. Native, because a fleet's worth of cells cannot each hold a popover. */
function describe(cell: BoardDevice): string {
  const device = cell.device
  const lines = [
    `${device.node_name} · gpu${device.device_index} · ${device.device_class}`,
    gibibytes(device.memory_mib),
  ]
  if (device.mig_profile) lines.push(`mig ${device.mig_profile}`)
  if (device.health !== "healthy") {
    lines.push(
      `${device.health}${device.health_reason ? ` — ${device.health_reason}` : ""}`
    )
  }
  if (cell.cordoned) lines.push("node cordoned")
  if (cell.slices.length > 0) lines.push(`${cell.slices.length} slices`)
  if (cell.holder?.origin === "reserved") {
    /**
     * The caveat lives here and nowhere else, so it has to actually be said —
     * on every reserved cell, in the accessible name as well as the tooltip. The
     * board chose this square; the cluster has not, and a person reading the
     * picture without this sentence would take it for a placement.
     */
    lines.push(
      `reserved for ${cell.holder.workload_name}`,
      "declared, not yet bound — the cluster picks the actual devices"
    )
  } else if (cell.holder) {
    lines.push(
      `held by ${cell.holder.workload_name}${
        cell.holder.origin === "live" ? "" : ` (${cell.holder.origin})`
      }`
    )
  } else if (
    device.allocatable &&
    device.health === "healthy" &&
    !cell.cordoned
  ) {
    lines.push("free")
  }
  return lines.join("\n")
}
