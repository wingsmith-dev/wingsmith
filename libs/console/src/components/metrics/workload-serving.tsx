import type { WorkloadDeployment } from "@/modules/demand/types"

import { MetricsPanel } from "./metrics-panel"

/**
 * Serving measurement for a workload, which is every cluster's share drawn
 * separately.
 *
 * There is no workload scope in the metrics API and there should not be one. A
 * quantile does not average across clusters, per-stream throughput is what one
 * caller saw wherever it was served, and aggregate throughput is a different
 * number in each place — so one workload number would be three measurements
 * blended into something nobody could act on. Each share is asked for by its
 * own deployment and shown under the cluster that runs it.
 */
export function WorkloadServingSection({
  deployments,
  lead,
}: {
  deployments: Pick<WorkloadDeployment, "id" | "name" | "cluster_name">[]
  lead?: string[]
}) {
  if (deployments.length === 0) return null

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h2 className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
          Serving
        </h2>
        {deployments.length > 1 && (
          <p className="max-w-3xl text-2xs text-muted-foreground/50">
            One panel per cluster. These are not added up or averaged across
            clusters: each share is measured where it runs.
          </p>
        )}
      </div>
      {deployments.map((deployment) => (
        <div key={deployment.id} className="flex flex-col gap-1.5">
          <div className="font-mono text-2xs text-muted-foreground/60">
            {deployment.cluster_name}
          </div>
          <MetricsPanel
            scope="deployment"
            refId={deployment.id}
            family="inference"
            lead={lead}
          />
        </div>
      ))}
    </section>
  )
}
