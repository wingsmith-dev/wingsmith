import { Field } from "@/components/layout/field"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { TOPOLOGY_KIND_LABEL } from "@/modules/supply/display"
import type { TopologyDomain } from "@/modules/supply/types"

export function TopologyCard({ domains }: { domains: TopologyDomain[] }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Topology domains</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {domains.length === 0 && (
          <p className="font-mono text-2xs text-muted-foreground/60">
            No fabric reported for this node.
          </p>
        )}
        {domains.map((domain) => (
          <div key={domain.id} className="rounded-md border bg-background/40 p-3">
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-mono text-xs">{domain.id}</span>
              <span className="font-mono text-2xs text-muted-foreground/60">
                {TOPOLOGY_KIND_LABEL[domain.kind]}
              </span>
            </div>
            <div className="mt-2 grid gap-3 sm:grid-cols-3">
              <Field label="Interconnect">{domain.interconnect}</Field>
              <Field label="Healthy">
                {domain.devices_healthy}/{domain.device_count}
              </Field>
              <Field label="Allocated">{domain.devices_allocated}</Field>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
