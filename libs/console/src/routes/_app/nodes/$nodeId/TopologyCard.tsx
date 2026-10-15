import { Field } from "@/components/layout/field"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { topologyKindLabel } from "@/modules/supply/display"
import { useTopologyDomains } from "@/modules/supply/queries"

/**
 * Topology for one node, read from `/topology-domains?node=` — the standalone
 * collection is the authority, and a node payload carries no domains of its
 * own to inline.
 */
export function TopologyCard({ nodeId }: { nodeId: string }) {
  const { items, isLoading } = useTopologyDomains({ node: nodeId })

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Topology domains</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading && (
          <p className="font-mono text-2xs text-muted-foreground">Loading…</p>
        )}
        {items?.length === 0 && (
          <p className="font-mono text-2xs text-muted-foreground">
            No fabric reported for this node.
          </p>
        )}
        {items?.map((domain) => (
          <div
            key={domain.id}
            className="rounded-md border bg-background/40 p-3"
          >
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-mono text-xs">{domain.identifier}</span>
              <span className="font-mono text-2xs text-muted-foreground">
                {topologyKindLabel(domain.kind)}
              </span>
            </div>
            <div className="mt-2 grid gap-3 sm:grid-cols-3">
              <Field label="Devices">{domain.device_count}</Field>
              <Field label="Link">
                {domain.link_bandwidth_gbps
                  ? `${domain.link_bandwidth_gbps} Gb/s`
                  : "—"}
              </Field>
              <Field label="Spans nodes">
                {domain.spans_nodes ? "yes" : "no"}
              </Field>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
