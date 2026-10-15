import {
  DataTable,
  type Column,
  type ListState,
} from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { shortenId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { RoleReadinessLine } from "@/components/demand/serving-topology"
import { deploymentOperationalStatus } from "@/modules/demand/display"
import { roleReadiness } from "@/modules/demand/topology"
import type { WorkloadDeployment } from "@/modules/demand/types"
import { Link } from "@tanstack/react-router"

/**
 * The small run of vertical bars beside a ready count — one bar per share,
 * green where a replica is ready and red where the share is not yet filled.
 * Capped so a large share stays a glance rather than a ruler.
 */
function ReplicaBar({ ready, total }: { ready: number; total: number }) {
  const bars = Math.min(Math.max(total, 0), 12)
  if (bars === 0) return null
  return (
    <span className="inline-flex gap-px align-middle">
      {Array.from({ length: bars }, (_, i) => (
        <span
          key={i}
          className={cn(
            "h-3 w-1 rounded-[1px]",
            i < ready ? "bg-emerald-500" : "bg-destructive/70"
          )}
        />
      ))}
    </span>
  )
}

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
  servingDigest,
  bare,
}: {
  deployments: WorkloadDeployment[]
  unit: string
  /**
   * The configuration the workload currently declares. When given, adds a column saying which
   * configurations are actually up in each cluster and whether they are that one.
   */
  servingDigest?: string
  /** Inside a card, where the card's edge is the frame. */
  bare?: boolean
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
      columns={columns(unit, servingDigest)}
      list={list}
      rowKey={(deployment) => deployment.id}
      empty="Placement has not put this anywhere yet."
      bare={bare}
    />
  )
}

function columns(
  unit: string,
  servingDigest?: string
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
            <span className="block font-mono text-xs">
              {deployment.cluster_name}
            </span>
            <span className="block font-mono text-2xs text-muted-foreground">
              {shortenId(deployment.cluster_id)} ·{" "}
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
          className="block hover:underline"
        >
          <span className="block font-mono text-xs">{deployment.name}</span>
          <Id value={shortenId(deployment.id)} className="block" />
        </Link>
      ),
    },
    {
      key: "ready",
      header: `Ready (${unit})`,
      /**
       * Per role where the cluster reports roles, and the scalar rollup where it
       * does not.
       *
       * A group count against a scalar ready count is two numbers that are not
       * comparable — `2/1` on a 1:2 group is decode pods over groups — so the
       * moment roles arrive they replace the pair rather than sitting beside it.
       * The group total stays underneath, which is what the unit in the header
       * names.
       */
      cell: (deployment) => {
        const roles = roleReadiness(deployment.status)
        if (roles) {
          return (
            <div className="space-y-0.5">
              <RoleReadinessLine roles={roles} />
              <span className="block font-mono text-2xs text-muted-foreground">
                {deployment.desired_replicas} {unit}
              </span>
            </div>
          )
        }
        return (
          <span className="inline-flex items-center gap-2">
            <span className="font-mono text-xs">
              {deployment.replicas.ready}/{deployment.desired_replicas}
            </span>
            <ReplicaBar
              ready={deployment.replicas.ready}
              total={deployment.desired_replicas}
            />
          </span>
        )
      },
    },
    {
      key: "admission",
      header: "Status",
      cell: (deployment) => {
        const status = deploymentOperationalStatus(deployment)
        const detail = [status.reason, status.placement]
          .filter(Boolean)
          .join(" · ")
        return (
          <span title={detail || undefined}>
            <StatusLabel tone={status.tone}>{status.phase}</StatusLabel>
          </span>
        )
      },
    },
    {
      key: "generation",
      header: "At generation",
      align: "right",
      secondary: true,
      cell: (deployment) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {deployment.replicas.at_current_generation}/
          {deployment.replicas.total} · gen {deployment.generation}
        </span>
      ),
    },
    ...(servingDigest
      ? [
          {
            key: "configuration",
            header: "Running",
            align: "right" as const,
            secondary: true,
            /**
             * Which serving configurations this cluster actually has up, and how many replicas
             * are on each.
             *
             * Beside the generation rather than folded into it, because they answer different
             * questions: a generation moves when a share is rescaled or a workload is paused, and
             * neither changes the process. The digest only moves when the configuration does.
             *
             * Two rows here is a rollout in progress. One row that is not the declared digest is
             * a cluster still running what it was asked for last time — an agent that has not
             * reconnected, or an apply that has not landed. Nothing at all means no ready replica
             * has reported a digest, which is not the same as nothing running.
             */
            cell: (deployment: WorkloadDeployment) => {
              const entries = Object.entries(deployment.live_digests ?? {})
              if (!entries.length)
                return (
                  <span className="font-mono text-2xs text-muted-foreground">
                    <Dash>{null}</Dash>
                  </span>
                )
              return (
                <div className="space-y-0.5">
                  {entries.map(([digest, ready]) => (
                    <div
                      key={digest}
                      className={cn(
                        "font-mono text-2xs",
                        digest === servingDigest
                          ? "text-foreground"
                          : "text-muted-foreground"
                      )}
                    >
                      {ready} x {shortenId(digest)}
                      {digest === servingDigest ? "" : " (stale)"}
                    </div>
                  ))}
                </div>
              )
            },
          },
        ]
      : []),
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

  return rows
}
