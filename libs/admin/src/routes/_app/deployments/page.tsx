import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import {
  WORKLOAD_PHASES,
  WORKLOAD_TYPES,
  workloadPhaseTone,
} from "@/modules/demand/display"
import { Button } from "@/components/ui/button"
import { formatError } from "@/lib/format"
import { useDeleteDeployment, useDeployments } from "@/modules/demand/queries"
import type { Deployment } from "@/modules/demand/types"
import { DeploymentForm } from "./DeploymentForm"
import { useState } from "react"
import { toast } from "sonner"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

function columns(
  onEdit: (deployment: Deployment) => void,
  onDelete: (deployment: Deployment) => void
): Column<Deployment>[] {
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
          <Id value={deployment.id} className="ml-2" />
        </Link>
      </ResourceCell>
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
        {deployment.cluster_id}
      </Link>
    ),
  },
  {
    key: "type",
    header: "Type",
    cell: (deployment) => (
      <Badge variant="secondary" className="font-mono text-2xs">
        {deployment.workload.type}
      </Badge>
    ),
  },
  {
    key: "phase",
    header: "Phase",
    cell: (deployment) => (
      <StatusLabel tone={workloadPhaseTone(deployment.workload.phase)}>
        {deployment.workload.phase}
      </StatusLabel>
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
    key: "generation",
    header: "At generation",
    align: "right",
    secondary: true,
    cell: (deployment) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {deployment.replicas.at_current_generation}/{deployment.replicas.total} · gen{" "}
        {deployment.generation}
      </span>
    ),
  },
  {
    key: "priority",
    header: "Priority",
    secondary: true,
    cell: (deployment) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {deployment.workload.priority_class}
        {deployment.workload.preemptible && (
          <span className="text-muted-foreground/50"> · preemptible</span>
        )}
      </span>
    ),
  },
  {
    key: "reason",
    header: "Reason",
    cell: (deployment) => (
      <span className="font-mono text-2xs text-muted-foreground">
        <Dash>{deployment.workload.reason}</Dash>
      </span>
    ),
  },
  {
    key: "enabled",
    header: "Enabled",
    align: "right",
    cell: (deployment) => (
      <span className="font-mono text-2xs">
        {deployment.enabled ? "yes" : "no"}
      </span>
    ),
  },
  {
    key: "actions",
    header: "",
    align: "right",
    cell: (deployment) => (
      <RowActions
        actions={[
          { label: "Edit", onSelect: () => onEdit(deployment) },
          { label: "Delete", destructive: true, onSelect: () => onDelete(deployment) },
        ]}
      />
    ),
  },
  ]
}

/**
 * Spec, scheduling supertype and observed replicas are three different things,
 * and the columns keep them apart: `desired_replicas` is what we asked for,
 * `replicas.ready` is what the cluster reports, and `workload.phase` is what
 * placement decided.
 */
export function DeploymentsPage() {
  const search = useSearch({ from: "/_app/deployments" })
  const navigate = useNavigate({ from: "/deployments" })
  const { data: clusters } = useClusterOptions()
  const remove = useDeleteDeployment()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Deployment | undefined>()
  const [doomed, setDoomed] = useState<Deployment | undefined>()
  const list = useDeployments({
    cluster: search.cluster,
    project: search.project,
    phase: search.phase,
    type: search.type,
    recipe: search.recipe,
  })

  const set = (key: "cluster" | "phase" | "type") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Deployments"
        description="declared · recipe × cluster, with observed replicas beside it"
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            New deployment
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
            label="Phase"
            value={search.phase ?? ALL}
            onChange={set("phase")}
            options={WORKLOAD_PHASES.map((p) => ({ value: p, label: p }))}
          />
          <FilterSelect
            label="Type"
            value={search.type ?? ALL}
            onChange={set("type")}
            options={WORKLOAD_TYPES.map((t) => ({ value: t, label: t }))}
          />
        </div>

        <DataTable
          columns={columns(setEditing, setDoomed)}
          list={list}
          rowKey={(deployment) => deployment.id}
          empty="Nothing is deployed."
        />
      </div>

      {creating && (
        <DeploymentForm open onOpenChange={(open) => !open && setCreating(false)} />
      )}
      {editing && (
        <DeploymentForm
          key={editing.revision}
          open
          deployment={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="deployment"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="deployment"
        name={doomed?.name ?? ""}
        note="Its replicas are torn down and any endpoint target pointing at it stops routing."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Deployment deleted", { description: doomed.name })
            },
            onError: (e) => toast.error(formatError(e, "Could not delete deployment")),
          })
        }
      />
    </div>
  )
}
