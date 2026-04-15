import { DataTable } from "@/components/layout/data-table"
import { DeleteDialog } from "@/components/layout/destructive"
import { Field, FieldGrid } from "@/components/layout/field"
import { Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import { StatusLabel } from "@/components/layout/status-dot"
import { WriteAction } from "@/components/layout/write-action"
import {
  DrainWorkloadDialog,
  PauseWorkloadDialog,
  ScaleWorkloadDialog,
} from "@/components/demand/workload-actions"
import { WorkloadDeploymentsTable } from "@/components/demand/workload-deployments-table"
import { operationColumns } from "@/components/machinery/operation-columns"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import {
  parallelismLabel,
  splittable,
  workloadPhaseTone,
} from "@/modules/demand/display"
import {
  useDeleteWorkload,
  useRecipe,
  useWorkload,
} from "@/modules/demand/queries"
import type { WorkloadDeployment } from "@/modules/demand/types"
import { useOperations } from "@/modules/machinery/queries"
import { WorkloadForm } from "../WorkloadForm"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"

/**
 * One workload: what was asked for, and every cluster it landed on.
 *
 * The page a person lands on when they want to know how something is running.
 * There is deliberately no cluster field in its declared spec — a workload has
 * as many clusters as placement found, so the answer is the table under it, and
 * the deployment page is the drill-in for one of them.
 *
 * Scale, pause and drain are operations and are fired as operations: each one
 * gets an id and a phase and shows up in the list below, because "the plane
 * accepted the intent" and "the capacity exists" are different facts and only
 * the second one is what was asked.
 */
export function WorkloadDetailPage() {
  const { workloadId } = useParams({ from: "/_app/workloads/$workloadId" })
  const { data: workload, isLoading, error } = useWorkload(workloadId)
  const { data: recipe } = useRecipe(workload?.recipe_id ?? "")
  const operations = useOperations({ target: workload?.id })
  const remove = useDeleteWorkload()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)
  const [scaling, setScaling] = useState(false)
  const [pausing, setPausing] = useState(false)
  const [draining, setDraining] = useState<WorkloadDeployment | undefined>()
  const [drainOpen, setDrainOpen] = useState(false)
  const [tab, setTab] = useState("clusters")

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Workload" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load workload")}
        </div>
      </div>
    )
  }

  const scale = workload?.scale

  return (
    <div className="flex flex-col">
      <PageHeader
        title={workload?.name ?? "Workload"}
        description={workload?.id}
        readActions={
          <RefreshButton keys={[["workloads"], ["deployments"], ["operations"]]} />
        }
        actions={
          <div className="flex gap-2">
            {workload && (
              <WriteAction>
                <div className="flex gap-2">
                  <Button size="sm" onClick={() => setScaling(true)}>
                    Scale
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setPausing(true)}
                  >
                    {workload.paused ? "Resume" : "Pause"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={workload.deployments.length === 0}
                    onClick={() => {
                      setDraining(undefined)
                      setDrainOpen(true)
                    }}
                  >
                    Drain
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setEditing(true)}
                  >
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setDoomed(true)}
                  >
                    Delete
                  </Button>
                </div>
              </WriteAction>
            )}
            <Button asChild variant="outline" size="sm">
              <Link to="/workloads">
                <IconArrowLeft className="size-4" />
                All workloads
              </Link>
            </Button>
          </div>
        }
      />

      {workload && editing && (
        <WorkloadForm
          key={workload.revision}
          open
          workload={workload}
          onOpenChange={(open) => !open && setEditing(false)}
        />
      )}
      {workload && scaling && (
        <ScaleWorkloadDialog
          key={`scale-${workload.revision}`}
          workload={workload}
          open
          onOpenChange={setScaling}
        />
      )}
      {workload && pausing && (
        <PauseWorkloadDialog
          workload={workload}
          open
          onOpenChange={setPausing}
        />
      )}
      {workload && drainOpen && (
        <DrainWorkloadDialog
          key={`drain-${draining?.id ?? "any"}`}
          workload={workload}
          deployment={draining}
          open
          onOpenChange={setDrainOpen}
        />
      )}
      {workload && (
        <DeleteDialog
          icon="workload"
          open={doomed}
          onOpenChange={setDoomed}
          kind="workload"
          name={workload.name}
          note="Every cluster it was expanded onto loses its deployment, and any endpoint target pointing at one stops routing."
          error={remove.error}
          pending={remove.isPending}
          onConfirm={() =>
            remove.mutate(workload.id, {
              onSuccess: () => {
                setDoomed(false)
                toast.success("Workload deleted", {
                  description: workload.name,
                })
                navigate({ to: "/workloads" })
              },
              onError: (e) =>
                toast.error(formatError(e, "Could not delete workload")),
            })
          }
        />
      )}

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {workload && scale && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <StatusLabel tone={workloadPhaseTone(workload.phase)}>
                {workload.phase}
              </StatusLabel>
              {workload.paused && (
                <StatusLabel tone="warn">paused</StatusLabel>
              )}
              <span className="font-mono text-2xs text-muted-foreground/60">
                {workload.kind} · {workload.deployments.length} cluster
                {workload.deployments.length === 1 ? "" : "s"}
              </span>
              <Labels labels={workload.labels} />
            </div>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">
                  Scale — totals in {scale.unit}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="space-y-1.5">
                  <div className="flex justify-between font-mono text-2xs text-muted-foreground/60">
                    <span>ready</span>
                    <span>
                      {scale.ready}/{scale.desired}
                    </span>
                  </div>
                  <Progress
                    value={
                      scale.desired === 0
                        ? 0
                        : (scale.ready / scale.desired) * 100
                    }
                    className="h-1.5"
                  />
                </div>
                <FieldGrid>
                  <Field label="Desired">
                    {scale.desired} {scale.unit}
                  </Field>
                  <Field label="Placed">
                    {scale.placed} {scale.unit}
                    {scale.placed < scale.desired && (
                      <span className="ml-2 text-amber-600 dark:text-amber-400">
                        short by {scale.desired - scale.placed}
                      </span>
                    )}
                  </Field>
                  <Field label="Bounds">
                    {scale.min}–{scale.max}
                    <span className="ml-2 text-muted-foreground/60">
                      validated, not autoscaled
                    </span>
                  </Field>
                  <Field label="Strategy">
                    {splittable(workload.kind)
                      ? workload.strategy
                      : "atomic — a gang is never split"}
                  </Field>
                </FieldGrid>
              </CardContent>
            </Card>

            <Tabs value={tab} onValueChange={setTab}>
              <TabsList>
                <TabsTrigger value="clusters">Clusters</TabsTrigger>
                <TabsTrigger value="spec">Spec</TabsTrigger>
                <TabsTrigger value="operations">Operations</TabsTrigger>
              </TabsList>

              <TabsContent value="clusters" className="mt-3 space-y-3">
                <p className="max-w-2xl text-xs text-muted-foreground">
                  One row per cluster placement chose. Every number here is that
                  cluster's share of the total above — an operator sees one
                  cluster and cannot tell a whole workload from a fragment of
                  one, which is the point.
                </p>
                <WorkloadDeploymentsTable
                  deployments={workload.deployments}
                  unit={scale.unit}
                  onDrain={(deployment) => {
                    setDraining(deployment)
                    setDrainOpen(true)
                  }}
                />
              </TabsContent>

              <TabsContent value="spec" className="mt-3 space-y-4">
                <div className="grid gap-4 lg:grid-cols-2">
                  <Card>
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm">Declared spec</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <FieldGrid>
                        <Field label="Kind">{workload.kind}</Field>
                        <Field label="Recipe">
                          <Link to="/recipes" className="hover:underline">
                            {recipe?.name ?? workload.recipe_id}
                          </Link>
                        </Field>
                        <Field label="Project">{workload.project_id}</Field>
                        <Field label="Priority class">
                          {workload.priority_class}
                        </Field>
                        <Field label="Preemptible">
                          {workload.preemptible ? "yes" : "no"}
                        </Field>
                        <Field label="Gang size">{workload.gang_size}</Field>
                        <Field label="Adapters">
                          {workload.adapter_artifact_ids.length || "none"}
                        </Field>
                        <Field label="Revision">{workload.revision}</Field>
                        <Field label="Single domain">
                          {workload.requires_single_topology_domain === null
                            ? "recipe decides"
                            : workload.requires_single_topology_domain
                              ? (workload.topology_domain_kind ?? "yes")
                              : "not required"}
                        </Field>
                        <Field label="Deadline">
                          {workload.deadline_at
                            ? formatAbsolute(workload.deadline_at)
                            : "none"}
                        </Field>
                      </FieldGrid>
                    </CardContent>
                  </Card>

                  <Card>
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm">Placement</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <FieldGrid>
                        <Field label="Strategy">{workload.strategy}</Field>
                        <Field label="Fan-out cap">
                          {workload.max_clusters ?? "none"}
                        </Field>
                        <Field label="Pinned cluster">
                          {workload.pinned_cluster_id ? (
                            <Link
                              to="/clusters/$clusterId"
                              params={{
                                clusterId: workload.pinned_cluster_id,
                              }}
                              className="hover:underline"
                            >
                              {workload.pinned_cluster_id}
                            </Link>
                          ) : (
                            "none — placement chooses"
                          )}
                        </Field>
                        <Field label="Cluster floors">
                          {Object.keys(workload.cluster_floors).length === 0
                            ? "none"
                            : Object.entries(workload.cluster_floors)
                                .map(([uid, floor]) => `${uid}: ${floor}`)
                                .join(", ")}
                        </Field>
                        <Field label="Started">
                          {formatRelative(workload.started_at)}
                        </Field>
                        <Field label="Finished">
                          {formatRelative(workload.finished_at)}
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

              <TabsContent value="operations" className="mt-3 space-y-3">
                <p className="max-w-2xl text-xs text-muted-foreground">
                  Scale, pause and drain are plane-local: they rewrite the
                  shares and are never dispatched to a cluster. A scale can sit
                  pending on quota indefinitely, which is exactly why it has a
                  phase and not a return code.
                </p>
                <DataTable
                  columns={operationColumns()}
                  list={operations}
                  rowKey={(operation) => operation.id}
                  empty="Nothing has been asked of this workload."
                />
              </TabsContent>
            </Tabs>
          </>
        )}
      </div>
    </div>
  )
}
