import { DevicesTable } from "@/components/supply/devices-table"
import { Badge } from "@/components/ui/badge"
import { TOPOLOGY_KIND_LABEL } from "@/modules/supply/display"
import type { Device, TopologyDomain } from "@/modules/supply/types"

const GANG_SIZE = 8

export function GroupedDevices({
  devices,
  domains,
}: {
  devices: Device[]
  domains: TopologyDomain[]
}) {
  const byDomain = new Map<string, Device[]>()
  for (const device of devices) {
    const key = device.topology_domain_id ?? "unassigned"
    const bucket = byDomain.get(key)
    if (bucket) bucket.push(device)
    else byDomain.set(key, [device])
  }

  return (
    <div className="space-y-5">
      {[...byDomain.entries()].map(([domainId, group]) => {
        const domain = domains.find((d) => d.id === domainId)
        const healthy = group.filter((d) => d.health === "healthy").length
        const free = group.filter(
          (d) => d.health === "healthy" && !d.allocation
        ).length

        return (
          <section key={domainId} className="space-y-2">
            <div className="flex flex-wrap items-baseline gap-3">
              <span className="font-mono text-xs">{domainId}</span>
              {domain && (
                <span className="font-mono text-2xs text-muted-foreground/60">
                  {TOPOLOGY_KIND_LABEL[domain.kind]} · {domain.interconnect} ·{" "}
                  {domain.node_name}
                </span>
              )}
              <span className="font-mono text-2xs text-muted-foreground/60">
                {healthy}/{group.length} healthy · {free} free
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
            <DevicesTable
              devices={group}
              isLoading={false}
              showTopology={false}
            />
          </section>
        )
      })}
    </div>
  )
}
