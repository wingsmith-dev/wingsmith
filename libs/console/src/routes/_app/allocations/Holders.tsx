import { cn } from "@/lib/utils"
import { brickColor } from "@/modules/board/palette"
import { useBoardStore } from "@/modules/board/store"
import type { Board, Holder } from "@/modules/board/types"
import { Link } from "@tanstack/react-router"
import { useMemo } from "react"

interface Held {
  holder: Holder
  devices: number
  clusters: Set<string>
  fabrics: Set<string>
}

/**
 * Everything the board is holding, as a list — the colour key and the index at
 * once.
 *
 * Rolled up per workload rather than per allocation, because a workload with
 * eight replicas is one colour on the board and reading it back as eight rows
 * would answer a question nobody asked. `fabrics` is what makes a split gang
 * visible: one workload, one cluster, more than one fabric.
 */
export function Holders({ board }: { board: Board | undefined }) {
  const highlight = useBoardStore((s) => s.highlight)
  const highlightWorkload = useBoardStore((s) => s.highlightWorkload)

  const rows = useMemo(() => {
    const byWorkload = new Map<string, Held>()
    if (board) {
      for (const cell of board.devices.values()) {
        const holder = cell.holder
        if (!holder) continue
        const row = byWorkload.get(holder.workload)
        const fabric = board.fabric_of.get(cell.device.id) ?? ""
        if (row) {
          row.devices += 1
          row.clusters.add(cell.device.cluster_name)
          row.fabrics.add(fabric)
        } else {
          byWorkload.set(holder.workload, {
            holder,
            devices: 1,
            clusters: new Set([cell.device.cluster_name]),
            fabrics: new Set([fabric]),
          })
        }
      }
    }
    return [...byWorkload.values()].sort((a, b) => b.devices - a.devices)
  }, [board])

  if (rows.length === 0) {
    return (
      <p className="font-mono text-2xs text-muted-foreground/50">
        Nothing holds capacity in this scope.
      </p>
    )
  }

  return (
    <ul className="space-y-1">
      {rows.map((row) => {
        const colour = brickColor(row.holder.workload)
        const dim = !!highlight && highlight !== row.holder.workload
        return (
          <li
            key={row.holder.workload}
            onMouseEnter={() => highlightWorkload(row.holder.workload)}
            onMouseLeave={() => highlightWorkload(null)}
            className={cn(
              "flex items-center gap-2 rounded-md px-1.5 py-1 transition-opacity hover:bg-accent/40",
              dim && "opacity-40"
            )}
          >
            <span
              className={cn("size-2.5 shrink-0 rounded-[2px]", colour.swatch)}
            />
            <span className="min-w-0 flex-1">
              {row.holder.workload.startsWith("wkl_") ? (
                <Link
                  to="/workloads/$workloadId"
                  params={{ workloadId: row.holder.workload }}
                  className="block truncate font-mono text-2xs hover:underline"
                >
                  {row.holder.workload_name}
                </Link>
              ) : (
                <span className="block truncate font-mono text-2xs">
                  {row.holder.workload_name}
                </span>
              )}
              <span className="block truncate font-mono text-2xs text-muted-foreground/50">
                {row.holder.project_slug} · {[...row.clusters].join(", ")}
                {row.fabrics.size > 1 ? ` · ${row.fabrics.size} fabrics` : ""}
              </span>
            </span>
            <span className="shrink-0 font-mono text-2xs tabular-nums">
              {row.devices}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
