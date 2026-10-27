import { useState } from "react"

import { ModelCard } from "@/components/serving/model-card"
import { WindowToggle } from "@/components/metrics/window-toggle"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import {
  DEFAULT_SERVING_WINDOW,
  type TimeWindow,
} from "@/modules/metrics/windows"
import { useServingOverview } from "@/modules/serving/queries"

/**
 * The tenant's own dashboard, rendered inside the console for whoever operates
 * it. Same read and the same card, taken from `/tenants/{ref}/serving` (a
 * surface only an admin can reach), so what an operator sees on this page is
 * what the customer is looking at instead of an admin-side approximation of
 * it that drifts the first time either one is changed.
 */
export function TenantServed({ tenant }: { tenant: string }) {
  const overview = useServingOverview(tenant)
  const [window, setWindow] = useState<TimeWindow>(DEFAULT_SERVING_WINDOW)
  const models = overview.data?.models ?? []

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
          Served models · what this tenant sees
        </h2>
        <WindowToggle value={window} onChange={setWindow} />
      </div>

      {overview.isError ? (
        <Card size="sm" className="px-4 py-3">
          <p className="text-xs text-destructive">
            {formatError(overview.error, "Failed to load the serving view")}
          </p>
        </Card>
      ) : overview.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : models.length === 0 ? (
        <Card size="sm" className="px-4 py-8">
          <p className="text-center text-xs text-muted-foreground">
            No models are published to this tenant yet.
          </p>
        </Card>
      ) : (
        <div className="space-y-4">
          {models.map((model) => (
            <ModelCard
              key={model.id}
              model={model}
              window={window}
              tenant={tenant}
            />
          ))}
        </div>
      )}
    </div>
  )
}
