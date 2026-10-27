import { Field, FieldGrid } from "@/components/layout/field"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { brickColor } from "@/modules/board/palette"
import { brickOfHolder, heldBy } from "@/modules/board/sandbox"
import { useBoardStore, type Hand } from "@/modules/board/store"
import type { Board, BoardDevice, Brick, Holder } from "@/modules/board/types"
import type { Reservation } from "@/modules/placement/types"
import {
  deviceHealthTone,
  gibibytes,
  topologyKindLabel,
} from "@/modules/supply/display"
import { IconExternalLink } from "@tabler/icons-react"
import { Link } from "@tanstack/react-router"
import { Holders } from "./Holders"
import type { BoardMode } from "./route"

/**
 * What one cell of the board actually is.
 *
 * The board is deliberately terse — a device is 24 pixels — so everything that
 * cannot be a color lives here: the uuid, the fabric, the health reason, the
 * allocation holding it. In sandbox mode it is also where an allocation is
 * picked up or let go, since those need a name attached and a cell has no room
 * for one.
 */
export function Inspector({
  board,
  mode,
}: {
  board: Board | undefined
  mode: BoardMode
}) {
  const selection = useBoardStore((s) => s.selection)
  const select = useBoardStore((s) => s.select)

  if (!selection) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">On the board</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <Holders board={board} />
          <p className="font-mono text-2xs leading-relaxed text-muted-foreground">
            Click a device, a fabric or a cluster to inspect it.
          </p>
        </CardContent>
      </Card>
    )
  }

  const body = () => {
    if (selection.kind === "allocation") {
      const cells = [...(board?.devices.values() ?? [])].filter(
        (cell) => cell.holder?.allocation === selection.allocation
      )
      return cells.length > 0 && board ? (
        <AllocationDetail board={board} cells={cells} mode={mode} />
      ) : (
        <Missing what="allocation" />
      )
    }
    if (selection.kind === "device") {
      const cell = board?.devices.get(selection.id)
      return cell ? (
        <DeviceDetail board={board} cell={cell} mode={mode} />
      ) : (
        <Missing what="device" />
      )
    }
    if (selection.kind === "fabric") {
      const fabric = board?.fabrics.get(selection.key)
      return fabric ? (
        <FieldGrid>
          <Field label="Fabric">{fabric.label}</Field>
          <Field label="Nodes">{fabric.node_names.join(", ")}</Field>
          <Field label="Kind">{topologyKindLabel(fabric.kind)}</Field>
          <Field label="Cluster">{fabric.cluster_name}</Field>
          <Field label="Device class">{fabric.device_class ?? "mixed"}</Field>
          <Field label="Link">
            {fabric.domain?.link_bandwidth_gbps
              ? `${fabric.domain.link_bandwidth_gbps} GB/s${
                  fabric.domain.link_generation
                    ? ` · gen ${fabric.domain.link_generation}`
                    : ""
                }`
              : "—"}
          </Field>
          <Field label="Spans nodes">
            {fabric.domain?.spans_nodes ? `${fabric.nodes.length} nodes` : "no"}
          </Field>
          <Field label="Held">
            {fabric.counts.held}/{fabric.counts.total}
          </Field>
          <Field label="Free">{fabric.counts.free}</Field>
        </FieldGrid>
      ) : (
        <Missing what="fabric" />
      )
    }
    if (selection.kind === "cluster") {
      const cluster = board?.regions
        .flatMap((region) => region.clusters)
        .find((entry) => entry.cluster.id === selection.id)
      return cluster ? (
        <div className="space-y-3">
          <FieldGrid>
            <Field label="Cluster">{cluster.cluster.name}</Field>
            <Field label="Region">{cluster.cluster.region}</Field>
            <Field label="Phase">{cluster.cluster.phase}</Field>
            <Field label="Reachable">
              {cluster.cluster.reachable ? "yes" : "no"}
            </Field>
            <Field label="Fabrics">{cluster.fabrics.length}</Field>
            <Field label="Largest block">{cluster.largest_block}</Field>
            <Field label="Held">
              {cluster.counts.held}/{cluster.counts.total}
            </Field>
            <Field label="Free">{cluster.counts.free}</Field>
            {cluster.counts.reserved > 0 && (
              <>
                <Field label="Reserved">
                  <span className="text-primary">
                    {cluster.counts.reserved}
                  </span>
                </Field>
                <Field label="Available">{cluster.counts.available}</Field>
              </>
            )}
          </FieldGrid>
          <Incoming reservations={cluster.reservations} />
          <Link
            to="/clusters/$clusterId"
            params={{ clusterId: cluster.cluster.id }}
            className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
          >
            open cluster
            <IconExternalLink className="size-3" />
          </Link>
        </div>
      ) : (
        <Missing what="cluster" />
      )
    }
    const region = board?.regions.find(
      (entry) => entry.region === selection.region
    )
    return region ? (
      <div className="space-y-3">
        <FieldGrid>
          <Field label="Region">{region.region}</Field>
          <Field label="Clusters">
            {region.clusters.map((entry) => entry.cluster.name).join(", ")}
          </Field>
          <Field label="Devices">{region.counts.total}</Field>
          <Field label="Held">{region.counts.held}</Field>
          <Field label="Free">{region.counts.free}</Field>
          <Field label="Fault">{region.counts.fault}</Field>
          {region.counts.reserved > 0 && (
            <>
              <Field label="Reserved">
                <span className="text-primary">{region.counts.reserved}</span>
              </Field>
              <Field label="Available">{region.counts.available}</Field>
            </>
          )}
        </FieldGrid>
        <Incoming
          reservations={region.clusters.flatMap((entry) => entry.reservations)}
        />
      </div>
    ) : (
      <Missing what="region" />
    )
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="capitalize">{selection.kind}</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => select(null)}
            className="h-5 px-1.5 font-mono text-2xs text-muted-foreground"
          >
            clear
          </Button>
        </CardTitle>
      </CardHeader>
      <CardContent>{body()}</CardContent>
    </Card>
  )
}

/**
 * What is on its way here, named.
 *
 * The tally can only say "six reserved", and six devices missing from a cluster
 * that visibly has them is not an explanation until somebody can say what took
 * them. This is that list, and it is the whole reason a reserved number is worth
 * showing at all: the thing that spent the capacity is a deployment nobody
 * thought to look at.
 *
 * Every field below is one the plane actually stated — a cluster, a shape and a
 * count — which is what makes this the place to check when the cells on the
 * board and the reserved numeral do not agree. The cells are the board's guess
 * at where each of these will land; this list is what was really promised.
 */
function Incoming({ reservations }: { reservations: Reservation[] }) {
  if (reservations.length === 0) return null

  const rows = [...reservations].sort((a, b) => b.devices - a.devices)

  return (
    <div className="space-y-1">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        Incoming
      </div>
      {rows.map((reservation) => (
        <div
          key={`${reservation.cluster}:${reservation.deployment ?? reservation.workload}`}
          className="space-y-0.5 rounded-md bg-primary/5 px-1.5 py-1"
        >
          <div className="flex items-baseline gap-2 font-mono text-2xs">
            {reservation.workload.startsWith("wkl_") ? (
              <Link
                to="/workloads/$workloadId"
                params={{ workloadId: reservation.workload }}
                className="min-w-0 flex-1 truncate font-semibold hover:underline"
              >
                {reservation.workload_name ?? reservation.workload}
              </Link>
            ) : (
              <span className="min-w-0 flex-1 truncate font-semibold">
                {reservation.workload_name ?? reservation.workload}
              </span>
            )}
            <span className="shrink-0 text-primary tabular-nums">
              {reservation.devices}
            </span>
          </div>
          <div className="truncate font-mono text-2xs text-muted-foreground">
            {[
              reservation.deployment_name,
              reservation.device_class,
              reservation.requires_single_domain
                ? `one ${reservation.domain_kind ?? "fabric"}`
                : "may spread",
            ]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
      ))}
      <p className="font-mono text-2xs leading-relaxed text-muted-foreground">
        Declared, not yet bound. The cells marked for these on the board are the
        board&rsquo;s own guess; the cluster picks the actual accelerators.
      </p>
    </div>
  )
}

function DeviceDetail({
  board,
  cell,
  mode,
}: {
  board: Board | undefined
  cell: BoardDevice
  mode: BoardMode
}) {
  const device = cell.device
  const pickUp = useBoardStore((s) => s.pickUp)
  const release = useBoardStore((s) => s.release)
  const inspect = useBoardStore((s) => s.inspect)

  return (
    <div className="space-y-3">
      <FieldGrid>
        <Field label="Node">{device.node_name}</Field>
        <Field label="Index">gpu{device.device_index}</Field>
        <Field label="Class">{device.device_class}</Field>
        <Field label="Memory">{gibibytes(device.memory_mib)}</Field>
        <Field label="Health">
          <StatusLabel tone={deviceHealthTone(device.health)}>
            {device.health}
          </StatusLabel>
        </Field>
        <Field label="Schedulable">
          {cell.cordoned
            ? "no · node cordoned"
            : device.allocatable
              ? "yes"
              : "no"}
        </Field>
        <Field label="Fabric">
          {board?.fabrics.get(board.fabric_of.get(device.id) ?? "")?.label ??
            "none"}
        </Field>
        <Field label="MIG">
          {cell.slices.length > 0
            ? `${cell.slices.length} slices`
            : (device.mig_profile ?? "off")}
        </Field>
      </FieldGrid>

      {device.health_reason && (
        <Field label="Health reason">
          <span className="text-destructive">{device.health_reason}</span>
        </Field>
      )}

      <Field label="UUID">
        <span className="text-2xs text-muted-foreground">
          {device.device_uuid}
        </span>
      </Field>

      {cell.slices.length > 0 && (
        <div className="space-y-1">
          <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
            Slices
          </div>
          {cell.slices.map((slice) => (
            <div
              key={slice.device.id}
              className="flex items-center gap-2 rounded-md bg-muted/30 px-1.5 py-1 font-mono text-2xs"
            >
              <span
                className={cn(
                  "size-2 shrink-0 rounded-[2px]",
                  slice.holder
                    ? brickColor(slice.holder.workload).swatch
                    : "bg-muted-foreground/25",
                  slice.holder?.origin === "reserved" && "opacity-40"
                )}
              />
              <span className="flex-1 truncate">
                {slice.device.mig_profile ??
                  `slice ${slice.device.device_index}`}
              </span>
              <span className="truncate text-muted-foreground">
                {slice.holder
                  ? `${slice.holder.workload_name}${
                      slice.holder.origin === "reserved" ? " · reserved" : ""
                    }`
                  : "free"}
              </span>
            </div>
          ))}
        </div>
      )}

      {cell.holder?.origin === "reserved" && (
        <ReservedBlock holder={cell.holder} />
      )}

      {cell.holder && cell.holder.origin !== "reserved" && (
        <HolderBlock
          holder={cell.holder}
          onInspect={() => inspect(cell.holder!.allocation)}
          onPickUp={
            mode === "sandbox" && board
              ? () => take(board, cell.holder!, pickUp)
              : undefined
          }
          onRelease={
            mode === "sandbox" && board
              ? () =>
                  release(
                    cell.holder!.allocation,
                    cell.holder!,
                    heldBy(board, cell.holder!.allocation)
                  )
              : undefined
          }
        />
      )}

      <div className="flex gap-3">
        <Link
          to="/devices/$deviceId"
          params={{ deviceId: device.id }}
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          open device
          <IconExternalLink className="size-3" />
        </Link>
        <Link
          to="/nodes/$nodeId"
          params={{ nodeId: device.node }}
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          open node
          <IconExternalLink className="size-3" />
        </Link>
      </div>
    </div>
  )
}

/**
 * Everything committed together, as one thing.
 *
 * This is the panel behind the ordinary click, and it is a different question
 * from the device panel beside it: not "what is this accelerator" but "what is
 * running in here, and how much of the fleet does it have". So it counts the
 * devices, names the nodes and fabrics they are spread over, and leaves the
 * uuid and the health reason to the device panel a second click away.
 */
function AllocationDetail({
  board,
  cells,
  mode,
}: {
  board: Board
  cells: BoardDevice[]
  mode: BoardMode
}) {
  const pickUp = useBoardStore((s) => s.pickUp)
  const release = useBoardStore((s) => s.release)
  const inspect = useBoardStore((s) => s.inspect)
  const select = useBoardStore((s) => s.select)
  const holder = cells[0].holder!

  const nodes = [...new Set(cells.map((cell) => cell.device.node_name))]
  const fabrics = [
    ...new Set(
      cells
        .map((cell) => board.fabric_of.get(cell.device.id))
        .filter((key): key is string => !!key)
    ),
  ]

  return (
    <div className="space-y-3">
      <FieldGrid>
        <Field label="Cluster">{cells[0].device.cluster_name}</Field>
        <Field label="Devices">
          {cells.length}× {holder.device_class}
        </Field>
        <Field label="Nodes">{nodes.join(", ")}</Field>
        <Field label="Fabrics">
          {fabrics
            .map((key) => board.fabrics.get(key)?.label ?? key)
            .join(", ")}
        </Field>
      </FieldGrid>

      {fabrics.length > 1 && (
        <p className="font-mono text-2xs text-warning">
          This gang is split over {fabrics.length} fabrics.
        </p>
      )}

      {holder.origin === "reserved" ? (
        <ReservedBlock holder={holder} />
      ) : (
        <HolderBlock
          holder={holder}
          onInspect={() => inspect(holder.allocation)}
          onPickUp={
            mode === "sandbox" ? () => take(board, holder, pickUp) : undefined
          }
          onRelease={
            mode === "sandbox"
              ? () =>
                  release(
                    holder.allocation,
                    holder,
                    heldBy(board, holder.allocation)
                  )
              : undefined
          }
        />
      )}

      <div className="space-y-1">
        <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          Held devices
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-0.5">
          {cells.map((cell) => (
            <button
              key={cell.device.id}
              type="button"
              onClick={() => select({ kind: "device", id: cell.device.id })}
              className="font-mono text-2xs text-muted-foreground hover:text-primary hover:underline"
            >
              {cell.device.node_name}/gpu{cell.device.device_index}
            </button>
          ))}
        </div>
      </div>

      {holder.workload.startsWith("wkl_") && (
        <Link
          to="/workloads/$workloadId"
          params={{ workloadId: holder.workload }}
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          open workload
          <IconExternalLink className="size-3" />
        </Link>
      )}
    </div>
  )
}

/**
 * Take a live allocation into the hand.
 *
 * It goes in as a brick *and* as where it came from, which is the whole
 * difference between moving something and cloning it: the fit checks only ever
 * see the brick, and the drop only knows to free the old devices because the
 * hand remembers them.
 */
function take(
  board: Board,
  holder: Holder,
  pickUp: (brick: Brick, hand: Hand) => void
) {
  pickUp(brickOfHolder(board, holder), {
    allocation: holder.allocation,
    holder,
    from: heldBy(board, holder.allocation),
  })
}

/**
 * A reserved cell, explained.
 *
 * Deliberately not `HolderBlock`. That block offers "pick up" and "release",
 * which are operations on an allocation, and a reservation has none — its
 * allocation id is one this board invented so the cells could be grouped. It has
 * no phase to report and no gang to be complete, and the one thing it does have
 * to say is that this square is a guess.
 */
function ReservedBlock({ holder }: { holder: Holder }) {
  const color = brickColor(holder.workload)
  return (
    <div className={cn("space-y-2 rounded-md p-2", color.wash)}>
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "ring-dashed size-2.5 shrink-0 rounded-[2px] ring-1 ring-inset",
            color.ghost,
            color.edge
          )}
        />
        <span className="min-w-0 flex-1 truncate font-mono text-2xs font-semibold">
          {holder.workload_name}
        </span>
        <span className="font-mono text-2xs text-primary">reserved</span>
      </div>
      <div className="font-mono text-2xs text-muted-foreground">
        {holder.device_count}× {holder.device_class} owed to this cluster
      </div>
      <p className="font-mono text-2xs leading-relaxed text-muted-foreground">
        Declared, not yet bound. The board picked this device to show the
        promise somewhere; the cluster chooses the actual accelerators and may
        well choose others.
      </p>
    </div>
  )
}

function HolderBlock({
  holder,
  onInspect,
  onPickUp,
  onRelease,
}: {
  holder: Holder
  onInspect?: () => void
  onPickUp?: () => void
  onRelease?: () => void
}) {
  const color = brickColor(holder.workload)
  return (
    <div className={cn("space-y-2 rounded-md p-2", color.wash)}>
      <div className="flex items-center gap-2">
        <span className={cn("size-2.5 shrink-0 rounded-[2px]", color.swatch)} />
        <span className="min-w-0 flex-1 truncate font-mono text-2xs font-semibold">
          {holder.workload_name}
        </span>
        <span className="font-mono text-2xs text-muted-foreground">
          {holder.origin === "live" ? holder.phase : holder.origin}
        </span>
      </div>
      <div className="font-mono text-2xs text-muted-foreground">
        {holder.project_slug} · {holder.device_count}× {holder.device_class} ·{" "}
        {holder.gang_complete ? "gang complete" : "gang partial"}
      </div>
      {onInspect && (
        <Button
          variant="outline"
          size="sm"
          onClick={onInspect}
          className="h-6 w-full font-mono text-2xs"
        >
          what&rsquo;s running
        </Button>
      )}
      {(onPickUp || onRelease) && (
        <div className="flex gap-1">
          {onPickUp && (
            <Button
              variant="outline"
              size="sm"
              onClick={onPickUp}
              className="h-6 flex-1 font-mono text-2xs"
            >
              pick up
            </Button>
          )}
          {onRelease && (
            <Button
              variant="outline"
              size="sm"
              onClick={onRelease}
              className="h-6 flex-1 font-mono text-2xs"
            >
              release
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

function Missing({ what }: { what: string }) {
  return (
    <p className="font-mono text-2xs text-muted-foreground">
      That {what} is not in the current scope.
    </p>
  )
}
