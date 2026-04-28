import { Field, FieldGrid } from "@/components/layout/field"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { brickColor } from "@/modules/board/palette"
import { brickOfHolder, heldBy } from "@/modules/board/sandbox"
import { useBoardStore } from "@/modules/board/store"
import type { Board, BoardDevice, Holder } from "@/modules/board/types"
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
 * cannot be a colour lives here: the uuid, the fabric, the health reason, the
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
          <p className="font-mono text-2xs leading-relaxed text-muted-foreground/40">
            Click a device, a fabric or a cluster to inspect it.
          </p>
        </CardContent>
      </Card>
    )
  }

  const body = () => {
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
          </FieldGrid>
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
      <FieldGrid>
        <Field label="Region">{region.region}</Field>
        <Field label="Clusters">
          {region.clusters.map((entry) => entry.cluster.name).join(", ")}
        </Field>
        <Field label="Devices">{region.counts.total}</Field>
        <Field label="Held">{region.counts.held}</Field>
        <Field label="Free">{region.counts.free}</Field>
        <Field label="Fault">{region.counts.fault}</Field>
      </FieldGrid>
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
  const arm = useBoardStore((s) => s.arm)
  const release = useBoardStore((s) => s.release)

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
        <span className="text-2xs text-muted-foreground/70">
          {device.device_uuid}
        </span>
      </Field>

      {cell.slices.length > 0 && (
        <div className="space-y-1">
          <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
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
                    : "bg-muted-foreground/25"
                )}
              />
              <span className="flex-1 truncate">
                {slice.device.mig_profile ??
                  `slice ${slice.device.device_index}`}
              </span>
              <span className="truncate text-muted-foreground/60">
                {slice.holder?.workload_name ?? "free"}
              </span>
            </div>
          ))}
        </div>
      )}

      {cell.holder && (
        <HolderBlock
          holder={cell.holder}
          onPickUp={
            mode === "sandbox" && board
              ? () => arm(brickOfHolder(board, cell.holder!))
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

function HolderBlock({
  holder,
  onPickUp,
  onRelease,
}: {
  holder: Holder
  onPickUp?: () => void
  onRelease?: () => void
}) {
  const colour = brickColor(holder.workload)
  return (
    <div className={cn("space-y-2 rounded-md p-2", colour.wash)}>
      <div className="flex items-center gap-2">
        <span
          className={cn("size-2.5 shrink-0 rounded-[2px]", colour.swatch)}
        />
        <span className="min-w-0 flex-1 truncate font-mono text-2xs font-semibold">
          {holder.workload_name}
        </span>
        <span className="font-mono text-2xs text-muted-foreground/60">
          {holder.origin === "live" ? holder.phase : holder.origin}
        </span>
      </div>
      <div className="font-mono text-2xs text-muted-foreground/60">
        {holder.project_slug} · {holder.device_count}× {holder.device_class} ·{" "}
        {holder.gang_complete ? "gang complete" : "gang partial"}
      </div>
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
    <p className="font-mono text-2xs text-muted-foreground/50">
      That {what} is not in the current scope.
    </p>
  )
}
