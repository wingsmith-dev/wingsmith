import { Card, CardContent } from "@/components/ui/card"
import { TOPOLOGY_KIND_LABEL } from "@/modules/supply/display"
import type { TopologyDomain } from "@/modules/supply/types"

const GANG_SIZE = 8

/**
 * Answers the question the object model says this UI must answer: how many
 * topology domains currently hold a full gang of healthy, unallocated devices.
 */
export function TopologySummary({ domains }: { domains: TopologyDomain[] }) {
  const full = domains.filter((d) => d.devices_healthy >= GANG_SIZE)
  const free = domains.filter(
    (d) => d.devices_healthy - d.devices_allocated >= GANG_SIZE
  )
  const kinds = [...new Set(domains.map((d) => d.kind))]

  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-x-10 gap-y-3 py-4">
        <Stat label="Topology domains" value={String(domains.length)} />
        <Stat
          label={`Domains with ${GANG_SIZE} healthy`}
          value={`${full.length}`}
        />
        <Stat
          label={`Domains with ${GANG_SIZE} free`}
          value={`${free.length}`}
          tone={free.length === 0 ? "danger" : "ok"}
        />
        <Stat
          label="Fabrics"
          value={kinds.map((k) => TOPOLOGY_KIND_LABEL[k]).join(" · ") || "—"}
        />
      </CardContent>
    </Card>
  )
}

function Stat({
  label,
  value,
  tone = "default",
}: {
  label: string
  value: string
  tone?: "default" | "ok" | "danger"
}) {
  const toneClass =
    tone === "ok"
      ? "text-emerald-600 dark:text-emerald-400"
      : tone === "danger"
        ? "text-destructive"
        : ""
  return (
    <div>
      <div className="font-mono text-2xs uppercase tracking-wider text-muted-foreground/60">
        {label}
      </div>
      <div className={`mt-0.5 font-mono text-sm ${toneClass}`}>{value}</div>
    </div>
  )
}
