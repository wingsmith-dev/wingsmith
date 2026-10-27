import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { WORKLOAD_PHASES, workloadPhaseTone } from "@/modules/demand/display"
import { shortenId } from "@/lib/format"
import { useDeployments } from "@/modules/demand/queries"
import type { Deployment } from "@/modules/demand/types"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

function columns(clusterName: (id: string) => string): Column<Deployment>[] {
  return [
    {
      key: "name",
      header: "Deployment",
      cell: (deployment) => (
        <ResourceCell kind="deployment">
          <Link
            to="/deployments/$deploymentId"
            params={{ deploymentId: deployment.id }}
            className="block"
          >
            <span className="font-mono text-xs">{deployment.name}</span>
            <Id value={shortenId(deployment.id)} className="ml-2" />
          </Link>
        </ResourceCell>
      ),
    },
    {
      key: "workload",
      header: "Workload",
      cell: (deployment) => (
        <Link
          to="/workloads/$workloadId"
          params={{ workloadId: deployment.workload_id }}
          className="font-mono text-xs hover:underline"
        >
          {deployment.workload_name}
        </Link>
      ),
    },
    {
      key: "cluster",
      header: "Cluster",
      cell: (deployment) => (
        <Link
          to="/clusters/$clusterId"
          params={{ clusterId: deployment.cluster_id }}
          className="font-mono text-2xs text-muted-foreground hover:underline"
        >
          {clusterName(deployment.cluster_id)}
        </Link>
      ),
    },
    {
      key: "share",
      header: "Share",
      align: "right",
      cell: (deployment) => (
        <span className="font-mono text-xs">{deployment.desired_replicas}</span>
      ),
    },
    {
      key: "replicas",
      header: "Replicas ready",
      align: "right",
      cell: (deployment) => (
        <span className="font-mono text-xs">
          {deployment.replicas.ready}/{deployment.desired_replicas}
        </span>
      ),
    },
    {
      key: "phase",
      header: "Admission",
      cell: (deployment) => (
        <StatusLabel tone={workloadPhaseTone(deployment.admission_phase)}>
          {deployment.admission_phase}
        </StatusLabel>
      ),
    },
    {
      key: "reason",
      header: "Reason",
      cell: (deployment) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>{deployment.admission_reason}</Dash>
        </span>
      ),
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
}

/**
 * Every cluster's copy of every workload, flat.
 *
 * This page is a drill-down, not a place you create anything: you do not make a
 * deployment, placement makes one when it expands a workload onto a cluster. It
 * is here for the per-cluster question (what is this cluster carrying, and
 * what is stuck admitting on it), which the workload page cannot answer without
 * making the reader open every workload in turn.
 *
 * `desired_replicas` is this cluster's SHARE of its workload's total. It must
 * never be summed against a total or divided out of one.
 */
export function DeploymentsPage() {
  const search = useSearch({ from: "/_app/deployments" })
  const navigate = useNavigate({ from: "/deployments" })
  const { data: clusters } = useClusterOptions()
  const list = useDeployments({
    cluster: search.cluster,
    workload: search.workload,
    project: search.project,
    phase: search.phase,
  })

  const clusterName = (id: string) =>
    clusters?.find((cluster) => cluster.id === id)?.name ?? id

  const set = (key: "cluster" | "phase") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Deployments"
        description="declared · one workload's share on one cluster, with observed replicas beside it"
        actions={
          <Button asChild variant="outline">
            <Link to="/workloads">All workloads</Link>
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Cluster"
            value={search.cluster ?? ALL}
            onChange={set("cluster")}
            options={(clusters ?? []).map((cluster) => ({
              value: cluster.id,
              label: clusterLabel(cluster),
            }))}
          />
          <FilterSelect
            label="Admission"
            value={search.phase ?? ALL}
            onChange={set("phase")}
            options={WORKLOAD_PHASES.map((p) => ({ value: p, label: p }))}
          />
        </div>

        <DataTable
          columns={columns(clusterName)}
          list={list}
          rowKey={(deployment) => deployment.id}
          empty="No cluster is carrying anything."
        />
      </div>
    </div>
  )
}
