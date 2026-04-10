import { MetricsPanel } from "./metrics-panel"

import type { MetricScope } from "@/modules/metrics/types"

/** The panel with the heading the detail pages share. */
export function MetricsSection({
  scope,
  refId,
  family = "gpu",
  title,
  lead,
}: {
  scope: MetricScope
  refId: string | undefined
  family?: "gpu" | "inference"
  title?: string
  lead?: string[]
}) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {title ?? (family === "gpu" ? "Utilization" : "Serving")}
      </h2>
      <MetricsPanel scope={scope} refId={refId} family={family} lead={lead} />
    </section>
  )
}
