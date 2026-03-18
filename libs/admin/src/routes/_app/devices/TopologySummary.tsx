import { Card, CardContent } from "@/components/ui/card"
import { topologyKindLabel } from "@/modules/supply/display"
import type { TopologyDomain } from "@/modules/supply/types"

const GANG_SIZE = 8

/**
 * How much of the fleet can still take a tensor-parallel-8 job. Counted over
 * every domain in scope rather than the first page, because "are there eight
 * devices in one fabric" is an aggregate and a partial answer is a wrong one.
 */
export function TopologySummary({
  domains,
  isLoading,
}: {
  domains: TopologyDomain[] | undefined
  isLoading: boolean
}) {
  const all = domains ?? []
  const full = all.filter((d) => d.device_count >= GANG_SIZE)
  const kinds = [...new Set(all.map((d) => d.kind))]

  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-x-10 gap-y-3 py-4">
        <Stat label="Topology domains" value={isLoading ? "…" : String(all.length)} />
        <Stat
          label={`Domains of ${GANG_SIZE}+`}
          value={isLoading ? "…" : String(full.length)}
          tone={!isLoading && full.length === 0 ? "danger" : "ok"}
        />
        <Stat
          label="Spanning nodes"
          value={isLoading ? "…" : String(all.filter((d) => d.spans_nodes).length)}
        />
        <Stat
          label="Fabrics"
          value={kinds.map(topologyKindLabel).join(" · ") || "—"}
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
