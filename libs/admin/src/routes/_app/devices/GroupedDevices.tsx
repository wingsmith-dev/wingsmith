import { DataTable } from "@/components/layout/data-table"
import { deviceColumns } from "@/components/supply/columns"
import { Badge } from "@/components/ui/badge"
import { topologyKindLabel } from "@/modules/supply/display"
import type { Device } from "@/modules/supply/types"

const GANG_SIZE = 8

/**
 * Devices bucketed by the fabric they share. Grouping happens over what has
 * been loaded, and the header says so — a gang that looks incomplete may just
 * be a page boundary.
 */
export function GroupedDevices({ devices }: { devices: Device[] }) {
  const byDomain = new Map<string, Device[]>()
  for (const device of devices) {
    const key = device.topology?.identifier ?? "unassigned"
    const bucket = byDomain.get(key)
    if (bucket) bucket.push(device)
    else byDomain.set(key, [device])
  }

  return (
    <div className="space-y-5">
      {[...byDomain.entries()].map(([identifier, group]) => {
        const healthy = group.filter((d) => d.health === "healthy").length
        const free = group.filter((d) => d.allocatable).length
        const kind = group[0]?.topology?.kind

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
              {free >= GANG_SIZE && (
                <Badge
                  variant="outline"
                  className="border-emerald-500/40 bg-emerald-500/10 font-mono text-2xs text-emerald-600 dark:text-emerald-400"
                >
                  {GANG_SIZE}-way free
                </Badge>
              )}
            </div>
            <DataTable
              columns={deviceColumns({ showTopology: false })}
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
