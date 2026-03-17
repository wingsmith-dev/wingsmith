import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { formatPercent } from "@/lib/format"
import type { ClusterCapacity } from "@/modules/clusters/types"

function Meter({
  label,
  value,
  total,
}: {
  label: string
  value: number
  total: number
}) {
  const pct = total === 0 ? 0 : (value / total) * 100
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between">
        <span className="font-mono text-2xs uppercase tracking-wider text-muted-foreground/60">
          {label}
        </span>
        <span className="font-mono text-xs">
          {value}/{total}
          <span className="ml-2 text-muted-foreground/60">
            {formatPercent(value, total)}
          </span>
        </span>
      </div>
      <Progress value={pct} className="h-1.5" />
    </div>
  )
}

export function HealthCard({ capacity }: { capacity: ClusterCapacity }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Health &amp; capacity</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <Meter
          label="Nodes ready"
          value={capacity.nodes_ready}
          total={capacity.nodes_total}
        />
        <Meter
          label="Devices healthy"
          value={capacity.devices_healthy}
          total={capacity.devices_total}
        />
        <Meter
          label="Devices allocated"
          value={capacity.devices_allocated}
          total={capacity.devices_total}
        />
      </CardContent>
    </Card>
  )
}
