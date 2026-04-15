import { DataTable, type Column, type ListState } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { shortenId } from "@/lib/format"
import { workloadPhaseTone } from "@/modules/demand/display"
import type { WorkloadDeployment } from "@/modules/demand/types"
import { Link } from "@tanstack/react-router"

/**
 * Where a workload actually ended up, one row per cluster.
 *
 * This table is what replaced "Cluster" as a field on a workload. A workload
 * names no cluster — it names a total and the clusters it may use — so the
 * cluster question has as many answers as placement found, and the honest place
 * for them is a list under the spec rather than a single field pretending there
 * is one. Region rides along because "asia and europe" is how the ask was
 * phrased and a bare `clu_...` does not answer it.
 *
 * Every number here is THIS CLUSTER'S SHARE. Nothing on this row may be summed
 * against the workload's total or divided out of it; the rollup above the table
 * is the plane's own sum and the only one there is.
 */
export function WorkloadDeploymentsTable({
  deployments,
  unit,
  onDrain,
}: {
  deployments: WorkloadDeployment[]
  unit: string
  onDrain?: (deployment: WorkloadDeployment) => void
}) {
  const list: ListState<WorkloadDeployment> = {
    items: deployments,
    loaded: deployments.length,
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: () => {},
  }

  return (
    <DataTable
      columns={columns(unit, onDrain)}
      list={list}
      rowKey={(deployment) => deployment.id}
      empty="Placement has not put this anywhere yet."
    />
  )
}

function columns(
  unit: string,
  onDrain?: (deployment: WorkloadDeployment) => void
): Column<WorkloadDeployment>[] {
  const rows: Column<WorkloadDeployment>[] = [
    {
      key: "cluster",
      header: "Cluster",
      cell: (deployment) => (
        <ResourceCell kind="cluster">
          <Link
            to="/clusters/$clusterId"
            params={{ clusterId: deployment.cluster_id }}
            className="block"
          >
            <span className="font-mono text-xs">{deployment.cluster_name}</span>
            <span className="ml-2 font-mono text-2xs text-muted-foreground">
              <Dash>{deployment.region}</Dash>
            </span>
          </Link>
        </ResourceCell>
      ),
    },
    {
      key: "deployment",
      header: "Deployment",
      cell: (deployment) => (
        <Link
          to="/deployments/$deploymentId"
          params={{ deploymentId: deployment.id }}
          className="hover:underline"
        >
          <span className="font-mono text-xs">{deployment.name}</span>
          <Id value={shortenId(deployment.id)} className="ml-2" />
        </Link>
      ),
    },
    {
      key: "share",
      header: `Share (${unit})`,
      align: "right",
      cell: (deployment) => (
        <span className="font-mono text-xs">{deployment.desired_replicas}</span>
      ),
    },
    {
      key: "ready",
      header: "Ready",
      align: "right",
      cell: (deployment) => (
        <span className="font-mono text-xs">
          {deployment.replicas.ready}/{deployment.desired_replicas}
        </span>
      ),
    },
    {
      key: "admission",
      header: "Admission",
      cell: (deployment) => (
        <div className="space-y-0.5">
          <StatusLabel tone={workloadPhaseTone(deployment.admission_phase)}>
            {deployment.admission_phase}
          </StatusLabel>
          <div className="font-mono text-2xs text-muted-foreground">
            <Dash>{deployment.admission_reason}</Dash>
          </div>
        </div>
      ),
    },
    {
      key: "generation",
      header: "At generation",
      align: "right",
      secondary: true,
      cell: (deployment) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {deployment.replicas.at_current_generation}/{deployment.replicas.total}{" "}
          · gen {deployment.generation}
        </span>
      ),
    },
    {
      key: "enabled",
      header: "Enabled",
      align: "right",
      secondary: true,
      cell: (deployment) => (
        <span className="font-mono text-2xs">
          {deployment.enabled ? "yes" : "no"}
        </span>
      ),
    },
  ]

  if (onDrain) {
    rows.push({
      key: "actions",
      header: "",
      align: "right",
      cell: (deployment) => (
        <button
          type="button"
          className="font-mono text-2xs text-muted-foreground hover:text-destructive hover:underline"
          onClick={() => onDrain(deployment)}
        >
          Drain
        </button>
      ),
    })
  }

  return rows
}
