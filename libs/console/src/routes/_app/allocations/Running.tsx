import { Field, FieldGrid } from "@/components/layout/field"
import { StatusLabel } from "@/components/layout/status-dot"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"
import { brickColor } from "@/modules/board/palette"
import { useBoardStore } from "@/modules/board/store"
import type { Board, BoardDevice, Holder } from "@/modules/board/types"
import { useWorkload } from "@/modules/demand/queries"
import { allocationPhaseTone } from "@/modules/machinery/display"
import { useAllocation } from "@/modules/machinery/queries"
import { gibibytes } from "@/modules/supply/display"
import { IconExternalLink } from "@tabler/icons-react"
import { Link } from "@tanstack/react-router"
import { useMemo } from "react"

/** The way back to the object that asked for these devices. */
const AWAY =
  "inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"

/**
 * What is running here, in full.
 *
 * The board is a picture and the inspector is a margin note; neither has room
 * for the thing an operator actually wants once they have found the color they
 * were looking for, which workload this is, whose it is, when it took these
 * devices, which devices those are, and the way back to the object that asked
 * for them. That is a page's worth of answer for a click on a 24-pixel square,
 * so it opens over the board instead of pushing it around.
 *
 * The allocation is re-read from the plane, not taken off the board.
 * The board carries what it needs to paint (a holder, flattened) and the
 * fields here are the ones it drops: the replica, the acquisition time, the
 * members that have since been released. A dialog nobody has open costs
 * nothing, and one that is open is worth the request.
 */
export function Running({ board }: { board: Board | undefined }) {
  const inspecting = useBoardStore((s) => s.inspecting)
  const inspect = useBoardStore((s) => s.inspect)

  const cells = useMemo(() => {
    if (!board || !inspecting) return []
    return [...board.devices.values()].filter(
      (cell) => cell.holder?.allocation === inspecting
    )
  }, [board, inspecting])

  const holder = cells[0]?.holder ?? null

  return (
    <Dialog open={!!inspecting} onOpenChange={(open) => !open && inspect(null)}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        {holder ? (
          <Body board={board} holder={holder} cells={cells} />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle className="text-sm">Nothing here</DialogTitle>
              <DialogDescription className="font-mono text-2xs">
                That allocation is no longer on the board. It may have been
                released, or the filters may have moved past it.
              </DialogDescription>
            </DialogHeader>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Body({
  board,
  holder,
  cells,
}: {
  board: Board | undefined
  holder: Holder
  cells: BoardDevice[]
}) {
  /**
   * Only a holder the plane would recognize is asked about. `proposed` is a
   * sandbox invention and `reserved` wears an id this board made up so the cells
   * could be grouped: asking about either would be a 404 with a spinner in
   * front of it. Everything those two have to say is already in the holder.
   */
  const real = holder.origin === "live" || holder.origin === "moved"
  const allocation = useAllocation(real ? holder.allocation : "")
  const workload = useWorkload(
    holder.workload.startsWith("wkl_") ? holder.workload : ""
  )

  const color = brickColor(holder.workload)
  const row = allocation.data
  const cluster = cells[0]?.device.cluster ?? row?.cluster ?? null
  const clusterName = cells[0]?.device.cluster_name ?? row?.cluster_name ?? "—"

  /**
   * The deployment is workload × cluster, and neither the allocation nor the
   * board carries it, so it is found, not followed. A workload the plane
   * has not answered for yet simply has no link, which is better than a link
   * into a page that does not exist.
   */
  const deployment = workload.data?.deployments.find(
    (entry) => entry.cluster_id === cluster
  )

  const nodes = new Map<string, BoardDevice[]>()
  for (const cell of cells) {
    const bucket = nodes.get(cell.device.node_name)
    if (bucket) bucket.push(cell)
    else nodes.set(cell.device.node_name, [cell])
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2 text-sm">
          <span className={cn("size-3 shrink-0 rounded-[3px]", color.swatch)} />
          <span className="min-w-0 truncate">{holder.workload_name}</span>
        </DialogTitle>
        <DialogDescription className="font-mono text-2xs">
          {holder.origin === "live"
            ? "observed · these devices are committed together to this workload"
            : holder.origin === "reserved"
              ? "declared · owed to this cluster, and not yet bound to anything"
              : `${holder.origin} · this is the plan, and it is sent nowhere`}
        </DialogDescription>
      </DialogHeader>

      <FieldGrid>
        <Field label="Phase">
          <StatusLabel tone={allocationPhaseTone(row?.phase ?? holder.phase)}>
            {row?.phase ?? holder.phase}
          </StatusLabel>
        </Field>
        <Field label="Gang">
          {holder.origin === "reserved"
            ? "not yet bound"
            : (row?.gang_complete ?? holder.gang_complete)
              ? "complete"
              : `partial · ${cells.length} of ${row?.device_count ?? holder.device_count}`}
        </Field>
        <Field label="Project">
          {row?.project_slug ?? holder.project_slug}
        </Field>
        <Field label="Cluster">{clusterName}</Field>
        <Field label="Shape">
          {row?.device_count ?? holder.device_count}×{" "}
          {row?.device_class ?? holder.device_class}
        </Field>
        {real && (
          <>
            <Field label="Replica">{row?.replica ?? "—"}</Field>
            <Field label="Acquired">
              {row ? (
                <span title={formatAbsolute(row.acquired_at)}>
                  {formatRelative(row.acquired_at)}
                </span>
              ) : (
                "—"
              )}
            </Field>
            <Field label="Quota">
              {row?.over_quota ? (
                <span className="text-warning">over quota</span>
              ) : (
                "within quota"
              )}
            </Field>
          </>
        )}
      </FieldGrid>

      {holder.origin === "reserved" && (
        <p className="font-mono text-2xs leading-relaxed text-muted-foreground">
          The cells below are the board&rsquo;s own guess at where this will
          land. The count is exact and the cluster picks the accelerators.
        </p>
      )}

      {allocation.error && (
        <p className="font-mono text-2xs text-destructive">
          {formatError(allocation.error, "Could not read the allocation")}
        </p>
      )}

      <div className="space-y-1">
        <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          Devices · {cells.length}
        </div>
        {[...nodes.entries()].map(([node, group]) => (
          <div key={node} className="rounded-md bg-muted/30 px-2 py-1.5">
            <div className="flex items-baseline justify-between gap-2 font-mono text-2xs">
              <Link
                to="/nodes/$nodeId"
                params={{ nodeId: group[0].device.node }}
                className="min-w-0 truncate font-semibold hover:underline"
              >
                {node}
              </Link>
              <span className="shrink-0 text-muted-foreground">
                {group.length}× {group[0].device.device_class} ·{" "}
                {gibibytes(group[0].device.memory_mib)}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
              {group.map((cell) => (
                <Link
                  key={cell.device.id}
                  to="/devices/$deviceId"
                  params={{ deviceId: cell.device.id }}
                  className="font-mono text-2xs text-muted-foreground hover:text-primary hover:underline"
                >
                  gpu{cell.device.device_index}
                  {cell.device.mig_profile
                    ? ` · ${cell.device.mig_profile}`
                    : ""}
                </Link>
              ))}
            </div>
          </div>
        ))}
        {board && cells.length > 1 && fabricsOf(board, cells) > 1 && (
          <p className="font-mono text-2xs text-warning">
            This gang is split over {fabricsOf(board, cells)} fabrics.
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t pt-3">
        {holder.workload.startsWith("wkl_") && (
          <Link
            to="/workloads/$workloadId"
            params={{ workloadId: holder.workload }}
            className={AWAY}
          >
            open workload
            <IconExternalLink className="size-3" />
          </Link>
        )}
        {deployment && (
          <Link
            to="/deployments/$deploymentId"
            params={{ deploymentId: deployment.id }}
            className={AWAY}
          >
            open deployment
            <IconExternalLink className="size-3" />
          </Link>
        )}
        {cluster && (
          <Link
            to="/clusters/$clusterId"
            params={{ clusterId: cluster }}
            className={AWAY}
          >
            open cluster
            <IconExternalLink className="size-3" />
          </Link>
        )}
      </div>
    </>
  )
}

/** How many fabrics the gang landed in. One is the ordinary answer. */
function fabricsOf(board: Board, cells: BoardDevice[]): number {
  return new Set(cells.map((cell) => board.fabric_of.get(cell.device.id))).size
}
