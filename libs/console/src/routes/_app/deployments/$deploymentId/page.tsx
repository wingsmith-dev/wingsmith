import { Field, FieldGrid } from "@/components/layout/field"
import { Dash, Labels } from "@/components/layout/mono"
import { MetricsSection } from "@/components/metrics/metrics-section"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import { StatusLabel } from "@/components/layout/status-dot"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { NewBenchmarkDialog } from "@/components/benchmarks/new-benchmark"
import { benchmarkColumns } from "@/components/benchmarks/benchmark-columns"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useDeploymentBenchmarks } from "@/modules/benchmarks/queries"
import { useDeploymentEvents } from "@/modules/machinery/queries"
import { DeploymentTimeline } from "@/components/demand/deployment-timeline"
import { DataTable } from "@/components/layout/data-table"
import { operationColumns } from "@/components/machinery/operation-columns"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { parallelismLabel, workloadPhaseTone } from "@/modules/demand/display"
import {
  useDeployment,
  useRecipe,
  useWorkload,
} from "@/modules/demand/queries"
import { DeploymentLifecycle } from "@/components/demand/deployment-lifecycle"
import { useState } from "react"
import { useOperations } from "@/modules/machinery/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"

/**
 * The three numbers a serving deployment is judged on. They lead the panel at
 * full size; everything else in the family stays a tile.
 */
const LEAD_METRICS = [
  "inference.ttft",
  "inference.tpot",
  "inference.output_tokens_per_second",
]

export function DeploymentDetailPage() {
  const { deploymentId } = useParams({
    from: "/_app/deployments/$deploymentId",
  })
  const { data: deployment, isLoading, error } = useDeployment(deploymentId)
  const { data: workload } = useWorkload(deployment?.workload_id ?? "")
  const { data: recipe } = useRecipe(workload?.recipe_id ?? "")
  const operations = useOperations({ target: deployment?.id })
  const benchmarks = useDeploymentBenchmarks(deployment?.id)
  // Settled means "stop polling", not "healthy": a failed deployment has
  // finished moving too, and re-reading its log every five seconds forever
  // would ask the plane a question with a fixed answer.
  const events = useDeploymentEvents(
    deployment?.id,
    (deployment?.status?.ready_replicas ?? 0) > 0 ||
      deployment?.status?.phase === "failed"
  )
  const [tab, setTab] = useState("state")

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
        readActions={
          <RefreshButton
            keys={[
              ["deployments"],
              ["events"],
              ["operations"],
              ["benchmarks"],
              ["metrics"],
            ]}
          />
        }
        actions={
          <div className="flex gap-2">
            {deployment && (
              <Button asChild variant="outline">
                <Link
                  to="/workloads/$workloadId"
                  params={{ workloadId: deployment.workload_id }}
                >
                  Edit workload
                </Link>
              </Button>
            )}
            {deployment && (
              <NewBenchmarkDialog
                deployment={deployment.id}
                stayOnPage
                trigger={
                  <Button variant="outline">
                    Run benchmark
                  </Button>
                }
              />
            )}
            {deployment && (
              <NewOperationDialog
                defaultType="deployment.roll"
                defaultTarget={deployment.id}
                trigger={
                  <Button variant="outline">
                    Roll
                  </Button>
                }
              />
            )}
            <Button asChild variant="outline">
              <Link to="/deployments">
                <IconArrowLeft className="size-4" />
                All deployments
              </Link>
            </Button>
          </div>
        }
      />


      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {deployment && (
          <MetricsSection
            scope="deployment"
            refId={deployment.id}
            family="inference"
            lead={LEAD_METRICS}
          />
        )}

        {deployment && replicas && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <StatusLabel tone={workloadPhaseTone(deployment.admission_phase)}>
                {deployment.admission_phase}
              </StatusLabel>
              <span className="font-mono text-2xs text-muted-foreground/60">
                generation {deployment.generation}
              </span>
              <Labels labels={workload?.labels ?? {}} />
            </div>

            <DeploymentLifecycle deployment={deployment} />

            <Tabs value={tab} onValueChange={setTab}>
              <TabsList>
                <TabsTrigger value="state">State</TabsTrigger>
                <TabsTrigger value="activity">Activity</TabsTrigger>
                <TabsTrigger value="spec">Spec</TabsTrigger>
                <TabsTrigger value="benchmarks">Benchmarks</TabsTrigger>
              </TabsList>

              <TabsContent value="state" className="mt-3 space-y-4">
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
                            : (replicas.ready / deployment.desired_replicas) *
                              100
                        }
                        className="h-1.5"
                      />
                    </div>
                    <FieldGrid>
                      <Field label="Total">{replicas.total}</Field>
                      <Field label="Running">{replicas.running}</Field>
                      <Field label="Pending">{replicas.pending}</Field>
                      <Field label="Failed">{replicas.failed}</Field>
                      <Field label="At generation">
                        {replicas.at_current_generation}/{replicas.total}
                      </Field>
                      <Field label="Reason">
                        <Dash>{deployment.admission_reason}</Dash>
                      </Field>
                    </FieldGrid>
                  </CardContent>
                </Card>

                <div>
                  <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
                    Operations
                  </h2>
                  <DataTable
                    columns={operationColumns()}
                    list={operations}
                    rowKey={(operation) => operation.id}
                    empty="Nothing has been asked of this deployment."
                  />
                </div>
              </TabsContent>

              <TabsContent value="activity" className="mt-3 space-y-3">
                <DeploymentTimeline
                  events={events.items}
                  loading={events.isLoading}
                />
                {events.hasNextPage && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => events.fetchNextPage()}
                    disabled={events.isFetchingNextPage}
                  >
                    {events.isFetchingNextPage ? "Loading…" : "Show older"}
                  </Button>
                )}
              </TabsContent>

              <TabsContent value="spec" className="mt-3 space-y-4">
                <div className="grid gap-4 lg:grid-cols-2">
                  <Card>
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm">Placement</CardTitle>
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
                        <Field label="Share">
                          {deployment.desired_replicas} of{" "}
                          {workload?.scale.desired ?? "?"}
                        </Field>
                        <Field label="Admission">
                          {deployment.admission_phase}
                        </Field>
                        <Field label="Authority">{deployment.authority}</Field>
                        <Field label="Enabled">
                          {deployment.enabled ? "yes" : "no"}
                        </Field>
                        <Field label="Queued">
                          {formatRelative(deployment.queued_at)}
                        </Field>
                        <Field label="Placed">
                          {formatRelative(deployment.placed_at)}
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
                        <Field label="Name">
                          <Link
                            to="/workloads/$workloadId"
                            params={{ workloadId: deployment.workload_id }}
                            className="hover:underline"
                          >
                            {deployment.workload_name}
                          </Link>
                        </Field>
                        <Field label="Kind">{workload?.kind}</Field>
                        <Field label="Phase">{workload?.phase}</Field>
                        <Field label="Priority class">
                          {workload?.priority_class}
                        </Field>
                        <Field label="Preemptible">
                          {workload?.preemptible ? "yes" : "no"}
                        </Field>
                        <Field label="Gang size">{workload?.gang_size}</Field>
                        <Field label="Project">{workload?.project_id}</Field>
                        <Field label="Recipe">
                          <Link to="/recipes" className="hover:underline">
                            {recipe?.name ?? workload?.recipe_id}
                          </Link>
                        </Field>
                        <Field label="Adapters">
                          {workload?.adapter_artifact_ids.length || "none"}
                        </Field>
                        <Field label="Started">
                          {formatRelative(workload?.started_at ?? null)}
                        </Field>
                        <Field label="Reason" className="sm:col-span-2">
                          <Dash>{deployment.admission_reason}</Dash>
                        </Field>
                        <Field label="Deadline" className="sm:col-span-2">
                          {workload?.deadline_at
                            ? formatAbsolute(workload.deadline_at)
                            : "none"}
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
                          {recipe.shape.device_count}×{" "}
                          {recipe.shape.device_class}
                        </Field>
                        <Field label="Parallelism">
                          {parallelismLabel(recipe.parallelism)}
                        </Field>
                        <Field label="Context length">
                          {recipe.context_length}
                        </Field>
                        <Field label="Single domain">
                          {recipe.shape.requires_single_topology_domain
                            ? (recipe.shape.topology_domain_kind ?? "yes")
                            : "not required"}
                        </Field>
                      </FieldGrid>
                    </CardContent>
                  </Card>
                )}
              </TabsContent>

              <TabsContent value="benchmarks" className="mt-3 space-y-3">
                <div className="flex items-start justify-between gap-4">
                  <p className="max-w-2xl text-xs text-muted-foreground">
                    Load is generated inside the cluster and aimed at its own
                    frontend — no inference traffic crosses the control plane.
                    Results are kept in the harness's own shape, so two runs
                    compare when their harness and version match.
                  </p>
                  <NewBenchmarkDialog deployment={deployment.id} stayOnPage />
                </div>
                <DataTable
                  columns={benchmarkColumns()}
                  list={benchmarks}
                  rowKey={(run) => run.id}
                  empty="This deployment has never been measured."
                />
              </TabsContent>
            </Tabs>
          </>
        )}
      </div>
    </div>
  )
}
