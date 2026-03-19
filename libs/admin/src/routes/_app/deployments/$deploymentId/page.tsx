import { Field, FieldGrid } from "@/components/layout/field"
import { Dash, Labels } from "@/components/layout/mono"
import { MetricsSection } from "@/components/metrics/metrics-section"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { DataTable } from "@/components/layout/data-table"
import { operationColumns } from "@/components/machinery/operation-columns"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { parallelismLabel, workloadPhaseTone } from "@/modules/demand/display"
import { DeleteDialog } from "@/components/layout/destructive"
import { useDeleteDeployment, useDeployment, useRecipe } from "@/modules/demand/queries"
import { DeploymentForm } from "../DeploymentForm"
import { useState } from "react"
import { toast } from "sonner"
import { useOperations } from "@/modules/machinery/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"

export function DeploymentDetailPage() {
  const { deploymentId } = useParams({ from: "/_app/deployments/$deploymentId" })
  const { data: deployment, isLoading, error } = useDeployment(deploymentId)
  const { data: recipe } = useRecipe(deployment?.recipe_id ?? "")
  const operations = useOperations({ target: deployment?.id })
  const remove = useDeleteDeployment()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Deployment" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load deployment")}
        </div>
      </div>
    )
  }

  const replicas = deployment?.replicas

  return (
    <div className="flex flex-col">
      <PageHeader
        title={deployment?.name ?? "Deployment"}
        description={deployment?.id}
        actions={
          <div className="flex gap-2">
            {deployment && (
              <>
                <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
                  Edit
                </Button>
                <Button size="sm" variant="outline" onClick={() => setDoomed(true)}>
                  Delete
                </Button>
              </>
            )}
            {deployment && (
              <NewOperationDialog
                defaultType="deployment.roll"
                defaultTarget={deployment.id}
                trigger={
                  <Button size="sm" variant="outline">
                    Roll
                  </Button>
                }
              />
            )}
            <Button asChild variant="outline" size="sm">
              <Link to="/deployments">
                <IconArrowLeft className="size-4" />
                All deployments
              </Link>
            </Button>
          </div>
        }
      />

      {deployment && editing && (
        <DeploymentForm
          key={deployment.revision}
          open
          deployment={deployment}
          onOpenChange={(open) => !open && setEditing(false)}
        />
      )}
      {deployment && (
        <DeleteDialog
        icon="deployment"
          open={doomed}
          onOpenChange={setDoomed}
          kind="deployment"
          name={deployment.name}
          note="Its replicas are torn down and any endpoint target pointing at it stops routing."
          error={remove.error}
          pending={remove.isPending}
          onConfirm={() =>
            remove.mutate(deployment.id, {
              onSuccess: () => {
                setDoomed(false)
                toast.success("Deployment deleted", { description: deployment.name })
                navigate({ to: "/deployments" })
              },
              onError: (e) =>
                toast.error(formatError(e, "Could not delete deployment")),
            })
          }
        />
      )}

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {deployment && (
          <MetricsSection
            scope="deployment"
            refId={deployment.id}
            family="inference"
          />
        )}

        {deployment && replicas && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <StatusLabel tone={workloadPhaseTone(deployment.workload.phase)}>
                {deployment.workload.phase}
              </StatusLabel>
              <span className="font-mono text-2xs text-muted-foreground/60">
                generation {deployment.generation}
              </span>
              <Labels labels={deployment.labels} />
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Declared spec</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Cluster">
                      <Link
                        to="/clusters/$clusterId"
                        params={{ clusterId: deployment.cluster_id }}
                        className="hover:underline"
                      >
                        {deployment.cluster_id}
                      </Link>
                    </Field>
                    <Field label="Recipe">
                      <Link
                        to="/recipes"
                        className="hover:underline"
                      >
                        {recipe?.name ?? deployment.recipe_id}
                      </Link>
                    </Field>
                    <Field label="Replicas">
                      {deployment.min_replicas}–{deployment.max_replicas}, desired{" "}
                      {deployment.desired_replicas}
                    </Field>
                    <Field label="Enabled">{deployment.enabled ? "yes" : "no"}</Field>
                    <Field label="Adapters">
                      {deployment.adapter_artifact_ids.length || "none"}
                    </Field>
                    <Field label="Revision">{deployment.revision}</Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Workload</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Type">{deployment.workload.type}</Field>
                    <Field label="Phase">{deployment.workload.phase}</Field>
                    <Field label="Priority class">
                      {deployment.workload.priority_class}
                    </Field>
                    <Field label="Preemptible">
                      {deployment.workload.preemptible ? "yes" : "no"}
                    </Field>
                    <Field label="Gang size">{deployment.workload.gang_size}</Field>
                    <Field label="Project">{deployment.workload.project_id}</Field>
                    <Field label="Queued">
                      {formatRelative(deployment.workload.queued_at)}
                    </Field>
                    <Field label="Started">
                      {formatRelative(deployment.workload.started_at)}
                    </Field>
                    <Field label="Reason" className="sm:col-span-2">
                      <Dash>{deployment.workload.reason}</Dash>
                    </Field>
                    <Field label="Deadline" className="sm:col-span-2">
                      {deployment.workload.deadline_at
                        ? formatAbsolute(deployment.workload.deadline_at)
                        : "none"}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Observed replicas</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="space-y-1.5">
                    <div className="flex justify-between font-mono text-2xs text-muted-foreground/60">
                      <span>ready</span>
                      <span>
                        {replicas.ready}/{deployment.desired_replicas}
                      </span>
                    </div>
                    <Progress
                      value={
                        deployment.desired_replicas === 0
                          ? 0
                          : (replicas.ready / deployment.desired_replicas) * 100
                      }
                      className="h-1.5"
                    />
                  </div>
                  <FieldGrid>
                    <Field label="Total">{replicas.total}</Field>
                    <Field label="Running">{replicas.running}</Field>
                    <Field label="Pending">{replicas.pending}</Field>
                    <Field label="Failed">{replicas.failed}</Field>
                    <Field label="At generation" className="sm:col-span-2">
                      {replicas.at_current_generation}/{replicas.total}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>
            </div>

            {recipe && (
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Recipe</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Name">
                      {recipe.name} v{recipe.version}
                    </Field>
                    <Field label="Engine">
                      {recipe.engine} {recipe.engine_version}
                    </Field>
                    <Field label="Shape">
                      {recipe.shape.device_count}× {recipe.shape.device_class}
                    </Field>
                    <Field label="Parallelism">
                      {parallelismLabel(recipe.parallelism)}
                    </Field>
                    <Field label="Context length">{recipe.context_length}</Field>
                    <Field label="Single domain">
                      {recipe.shape.requires_single_topology_domain
                        ? (recipe.shape.topology_domain_kind ?? "yes")
                        : "not required"}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>
            )}

            <div>
              <h2 className="mb-2 font-mono text-2xs uppercase tracking-[0.18em] text-muted-foreground/50">
                Operations
              </h2>
              <DataTable
                columns={operationColumns()}
                list={operations}
                rowKey={(operation) => operation.id}
                empty="Nothing has been asked of this deployment."
              />
            </div>
          </>
        )}
      </div>
    </div>
  )
}
