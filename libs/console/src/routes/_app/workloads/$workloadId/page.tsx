import {
  IconAdjustments,
  IconArrowLeft,
  IconChartHistogram,
  IconCpu,
  IconFlask,
  IconLayoutDashboard,
  IconTerminal2,
  IconTimeline,
  IconTrash,
} from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { NewRunDialog } from "@/components/benchmarks/new-run"
import { ActivityStream } from "@/components/demand/activity-stream"
import { DeploymentTimeline } from "@/components/demand/deployment-timeline"
import {
  CapabilitiesCard,
  ServingBehaviourCard,
} from "@/components/demand/serving-config"
import { RatioDialog, RolesPanel } from "@/components/demand/serving-topology"
import {
  DrainWorkloadDialog,
  PauseWorkloadDialog,
  ReshapeWorkloadDialog,
  ScaleWorkloadDialog,
} from "@/components/demand/workload-actions"
import { WorkloadDeploymentsTable } from "@/components/demand/workload-deployments-table"
import { WorkloadRevisions } from "@/components/demand/workload-revisions"
import { DataTable } from "@/components/layout/data-table"
import { DeleteDialog } from "@/components/layout/destructive"
import { Field, FieldGrid } from "@/components/layout/field"
import { CopyableId, Dash, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { Panel } from "@/components/layout/panel"
import { RefreshButton } from "@/components/layout/refresh-button"
import { StatRow, StatTile } from "@/components/layout/stat-tile"
import { WriteAction } from "@/components/layout/write-action"
import { operationColumns } from "@/components/machinery/operation-columns"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import {
  useServingBenchmarkSummary,
  useWorkloadBenchmarks,
  useWorkbenchCurve,
} from "@/modules/benchmarks/queries"
import {
  parallelismLabel,
  workloadOperationalStatus,
} from "@/modules/demand/display"
import {
  useArtifact,
  useDeleteWorkload,
  useEndpoints,
  useWorkload,
  useWorkloadPlacement,
} from "@/modules/demand/queries"
import {
  declaredRatio,
  isDisaggregated,
  scaleUnitFor,
} from "@/modules/demand/topology"
import type { Workload, WorkloadDeployment } from "@/modules/demand/types"
import { useOperations, useWorkloadEvents } from "@/modules/machinery/queries"
import { workloadEndpointColumns } from "./Columns"
import { SeeAllEvents } from "./Common"
import { type EditSection, EditButton } from "./EditButton"
import {
  EditCapabilitiesDialog,
  EditDetailsDialog,
  EditEngineArgsDialog,
  EditEngineDialog,
  EditPlacementDialog,
  EditServingDialog,
} from "./EditWorkload"
import { EngineArgsPanel } from "./EngineArgs"
import { PerformanceView } from "./Performance"
import { PlacementView } from "./Placement"
import { TrafficPanel } from "./Traffic"
import { Vitals } from "./Vitals"
import { WorkbenchView } from "./Workbench"

/**
 * One workload: what was asked for, and every cluster it landed on.
 *
 * The page a person lands on when they want to know how something is running.
 * There is deliberately no cluster field in its declared spec — a workload has
 * as many clusters as placement found, so the answer is the table under it, and
 * the deployment page is the drill-in for one of them.
 *
 * Scale, pause and drain are operations and are fired as operations: each one
 * gets an id and a phase and shows up under Lifecycle, because "the plane
 * accepted the intent" and "the capacity exists" are different facts and only
 * the second one is what was asked. Scaling is reached from the Deployments
 * panel header and the actions menu.
 *
 * The page is split into six views — overview, performance, workbench,
 * placement, lifecycle, logs. Events, operations, the endpoints routing to the
 * workload, its live placement, its performance across every cluster it landed
 * in and its benchmark run history are read from the plane. Anything the plane
 * does not report is left off the page, or drawn as an explicit "not measured
 * yet" frame — never as an invented figure.
 */
export function WorkloadDetailPage() {
  const { workloadId } = useParams({ from: "/_app/workloads/$workloadId" })
  const { data: workload, isLoading, error } = useWorkload(workloadId)
  const { data: artifact } = useArtifact(workload?.serving.artifact_id ?? "")
  const { data: placement } = useWorkloadPlacement(workloadId)
  const endpoints = useEndpoints({ workload: workload?.id })
  const deploymentIds = useMemo(
    () => (workload?.deployments ?? []).map((d) => d.id),
    [workload]
  )
  const { data: benchmarks } = useWorkloadBenchmarks(deploymentIds)
  /**
   * The most recent ramp read as a curve. Recovered from the runs rather than
   * listed — the plane has no request collection — and read once here so the
   * Workbench tiles and the curve panel share one query.
   */
  const curve = useWorkbenchCurve(benchmarks)
  /**
   * What this configuration has been measured at, asked by digest rather than by
   * workload. Safe to ask unconditionally — an unmeasured digest is a zero count,
   * not a 404.
   */
  const measured = useServingBenchmarkSummary(workload?.serving_digest)
  const operations = useOperations({ target: workload?.id })
  const settled =
    (workload?.phase === "running" || workload?.phase === "failed") &&
    workload.scale.ready >= workload.scale.desired
  const events = useWorkloadEvents(workload?.id, settled)
  const recentEvents = events.items?.slice(0, RECENT_EVENTS)
  const remove = useDeleteWorkload()
  const navigate = useNavigate()
  const [editing, setEditing] = useState<EditSection>(null)
  const [doomed, setDoomed] = useState(false)
  const [scaling, setScaling] = useState(false)
  const [pausing, setPausing] = useState(false)
  const [reshaping, setReshaping] = useState(false)
  const [ratioOpen, setRatioOpen] = useState(false)
  const [draining, setDraining] = useState<WorkloadDeployment | undefined>()
  const [drainOpen, setDrainOpen] = useState(false)
  const [view, setView] = useState("overview")
  const [benchOpen, setBenchOpen] = useState(false)

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
  const operational = workload ? workloadOperationalStatus(workload) : null

  return (
    <div className="flex flex-col">
      <PageHeader
        back={
          <>
            <Link
              to="/workloads"
              aria-label="All workloads"
              title="All workloads"
              className="flex size-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <IconArrowLeft className="size-4" />
            </Link>
            <span className="h-8 w-px bg-border" />
          </>
        }
        title={workload?.name ?? "Workload"}
        description={workload?.id}
        readActions={
          <RefreshButton
            keys={[["workloads"], ["deployments"], ["operations"], ["events"]]}
          />
        }
        actions={
          workload && (
            <div className="flex gap-2">
              <Button onClick={() => setScaling(true)}>Scale</Button>
              <Button variant="outline" onClick={() => setPausing(true)}>
                {workload.paused ? "Resume" : "Pause"}
              </Button>
              <Button
                variant="outline"
                disabled={workload.deployments.length === 0}
                onClick={() => {
                  setDraining(undefined)
                  setDrainOpen(true)
                }}
              >
                Drain
              </Button>
              {isDisaggregated(workload) && (
                <Button variant="outline" onClick={() => setRatioOpen(true)}>
                  Ratio
                </Button>
              )}
              <Button variant="outline" onClick={() => setReshaping(true)}>
                Reshape
              </Button>
              <Button variant="destructive" onClick={() => setDoomed(true)}>
                <IconTrash className="size-4" />
                Delete
              </Button>
            </div>
          )
        }
      />

      {workload && editing && (
        <EditDialog
          key={`${editing}-${workload.revision}`}
          section={editing}
          workload={workload}
          onClose={() => setEditing(null)}
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
      {workload && ratioOpen && (
        <RatioDialog
          workload={workload}
          budgetDevices={placement?.gpu_count}
          open
          onOpenChange={setRatioOpen}
        />
      )}
      {workload && reshaping && (
        <ReshapeWorkloadDialog
          key={`reshape-${workload.revision}`}
          workload={workload}
          open
          onOpenChange={setReshaping}
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
      {workload && benchOpen && (
        <NewRunDialog
          open
          level="workload"
          target={workload.id}
          onOpenChange={setBenchOpen}
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

      {isLoading && (
        <div className="p-4">
          <Skeleton className="h-40 w-full" />
        </div>
      )}

      {workload && scale && operational && (
        <Tabs value={view} onValueChange={setView} className="flex-col gap-0">
          <TabsList variant="underline" className="bg-card px-4">
            <ViewTab value="overview" icon={IconLayoutDashboard}>
              Overview
            </ViewTab>
            <ViewTab value="performance" icon={IconChartHistogram}>
              Performance
            </ViewTab>
            <ViewTab value="workbench" icon={IconFlask}>
              Workbench
            </ViewTab>
            <ViewTab value="placement" icon={IconCpu}>
              Placement
            </ViewTab>
            <ViewTab value="configuration" icon={IconAdjustments}>
              Configuration
            </ViewTab>
            <ViewTab value="lifecycle" icon={IconTimeline}>
              Lifecycle
            </ViewTab>
            <ViewTab value="logs" icon={IconTerminal2}>
              Logs
            </ViewTab>
          </TabsList>

          <div className="space-y-4 p-4">
            {/* ---- Overview ------------------------------------------ */}
            <TabsContent value="overview" className="space-y-4">
              <Vitals workload={workload} operational={operational} />

              <div className="grid gap-4 md:grid-cols-[2fr_1fr]">
                <div className="flex min-w-0 flex-col gap-4">
                  <TrafficPanel
                    workloadId={workload.id}
                    placed={workload.deployments.length > 0}
                  />

                  {/* On the overview and not only in the configuration tab.
                      Changing the ratio is the action this workload exists to
                      make cheap, and an action five tabs in is an action
                      nobody takes. */}
                  <RolesPanel
                    workload={workload}
                    budgetDevices={placement?.gpu_count}
                  />

                  <Panel
                    title="Deployments"
                    description={`${workload.deployments.length} cluster${
                      workload.deployments.length === 1 ? "" : "s"
                    } placement chose`}
                    action={
                      <WriteAction>
                        <Button size="sm" onClick={() => setScaling(true)}>
                          Scale
                        </Button>
                      </WriteAction>
                    }
                  >
                    <WorkloadDeploymentsTable
                      bare
                      deployments={workload.deployments}
                      unit={scaleUnitFor(workload)}
                      servingDigest={workload.serving_digest}
                    />
                  </Panel>

                  <Panel
                    title="Endpoints"
                    description="Entrypoints routing traffic to this workload"
                  >
                    <DataTable
                      bare
                      columns={workloadEndpointColumns}
                      list={endpoints}
                      rowKey={(e) => e.id}
                      empty="No endpoint routes to this workload."
                    />
                  </Panel>
                </div>

                <div className="flex min-w-0 flex-col gap-4">
                  {/* What is being served, in the six fields somebody asks for
                      without leaving the page. The whole serving block, the
                      engine's arguments and what the engine claims to support
                      are configuration, and configuration has its own view. */}
                  <Panel
                    title="Model"
                    action={
                      <div className="flex items-center gap-2">
                        <PanelLink onClick={() => setView("configuration")}>
                          Configuration
                        </PanelLink>
                        <EditButton
                          label="Edit engine and hardware"
                          onClick={() => setEditing("engine")}
                        />
                      </div>
                    }
                  >
                    <FieldGrid>
                      <Field label="Artifact">
                        <Link
                          to="/artifacts/$artifactId"
                          params={{ artifactId: workload.serving.artifact_id }}
                          className="hover:underline"
                        >
                          {artifact?.name ??
                            workload.artifact_tag ??
                            workload.serving.artifact_id}
                        </Link>
                      </Field>
                      <Field label="Engine">
                        {workload.serving.engine}{" "}
                        <span className="text-muted-foreground">
                          {workload.serving.engine_runtime_version}
                        </span>
                      </Field>
                      <Field label="Context length">
                        {workload.serving.context_length.toLocaleString()}
                      </Field>
                      <Field label="Quantization">
                        <Dash>
                          {[
                            workload.serving.quantization,
                            workload.serving.dtype,
                          ]
                            .filter(Boolean)
                            .join(" · ") || null}
                        </Dash>
                      </Field>
                      <Field label="Engine serves as" className="sm:col-span-2">
                        <span className="font-mono">
                          {workload.served_model_name || "—"}
                        </span>
                      </Field>
                    </FieldGrid>
                  </Panel>

                  <Panel
                    title="Details"
                    action={
                      <EditButton
                        label="Edit details"
                        onClick={() => setEditing("details")}
                      />
                    }
                  >
                    <FieldGrid>
                      <Field label="Kind">{workload.kind}</Field>
                      <Field label="Region">
                        <Dash>
                          {Array.from(
                            new Set(
                              workload.deployments
                                .map((d) => d.region)
                                .filter((r): r is string => r != null)
                            )
                          ).join(", ") || null}
                        </Dash>
                      </Field>
                      <Field label="Project" className="sm:col-span-2">
                        <CopyableId value={workload.project_id} />
                      </Field>
                      <Field label="Created" className="sm:col-span-2">
                        {formatAbsolute(workload.created_at)}
                      </Field>
                      <Field label="Labels" className="sm:col-span-2">
                        <Labels labels={workload.labels} />
                      </Field>
                    </FieldGrid>
                  </Panel>

                  <Panel
                    title="Activity"
                    action={<SeeAllEvents onClick={() => setView("logs")} />}
                  >
                    <DeploymentTimeline
                      events={recentEvents}
                      loading={events.isLoading}
                    />
                  </Panel>
                </div>
              </div>
            </TabsContent>

            {/* ---- Configuration ------------------------------------- */}
            <TabsContent value="configuration">
              <div className="grid items-start gap-4 md:grid-cols-[2fr_1fr]">
                <div className="flex min-w-0 flex-col gap-4">
                  <Panel
                    title="Serving & artifact"
                    description="How these weights are served. There is no object behind it — two workloads configured the same way carry the same digest."
                    action={
                      <EditButton
                        label="Edit engine and hardware"
                        onClick={() => setEditing("engine")}
                      />
                    }
                  >
                    <FieldGrid>
                      <Field label="Artifact">
                        <Link
                          to="/artifacts/$artifactId"
                          params={{ artifactId: workload.serving.artifact_id }}
                          className="hover:underline"
                        >
                          {artifact?.name ??
                            workload.artifact_tag ??
                            workload.serving.artifact_id}
                        </Link>
                      </Field>
                      {/* The name the ENGINE registers under, which is not the
                          workload's name and not what a client sends either.
                          A client sends the endpoint's slug; the gateway
                          rewrites it to this. The two are equal unless somebody
                          published an alias, and saying which is which here is
                          the point. */}
                      <Field label="Engine serves as">
                        <span className="font-mono">
                          {workload.served_model_name || "—"}
                        </span>
                        <span className="ml-2 text-2xs text-muted-foreground">
                          from the artifact&rsquo;s slug
                        </span>
                      </Field>
                      <Field label="Hardware">
                        {workload.serving.shape.device_count}×{" "}
                        {workload.serving.shape.device_class}
                      </Field>
                      <Field label="Engine">{workload.serving.engine}</Field>
                      <Field label="Engine runtime">
                        {workload.serving.engine_runtime_version}
                      </Field>
                      <Field label="Frontend image">
                        <Dash>{workload.serving.frontend_image}</Dash>
                      </Field>
                      <Field label="Parallelism">
                        {parallelismLabel(workload.serving.parallelism)}
                      </Field>
                      {/* Beside parallelism because it is the same kind of
                          fact: it decides the footprint. Not beside "Single
                          domain" two fields down, which reads `topology` in the
                          other sense — a physical fabric a replica has to fit
                          inside, not a split between two processes. */}
                      <Field label="Serving topology">
                        {isDisaggregated(workload) ? (
                          <span className="inline-flex items-center gap-2">
                            disaggregated
                            <span className="text-2xs text-muted-foreground">
                              {declaredRatio(workload)}
                            </span>
                          </span>
                        ) : (
                          "aggregated"
                        )}
                      </Field>
                      <Field label="Quantization">
                        <Dash>
                          {[
                            workload.serving.quantization,
                            workload.serving.dtype,
                          ]
                            .filter(Boolean)
                            .join(" · ") || null}
                        </Dash>
                      </Field>
                      <Field label="Context length">
                        {workload.serving.context_length.toLocaleString()}
                      </Field>
                      <Field label="Single domain">
                        {workload.serving.shape.requires_single_topology_domain
                          ? (workload.serving.shape.topology_domain_kind ??
                            "yes")
                          : "not required"}
                      </Field>
                      <Field label="Adapters">
                        {workload.adapter_artifact_ids.length || "none"}
                      </Field>
                      <Field label="Digest" className="sm:col-span-2">
                        <span className="font-mono text-2xs break-all text-muted-foreground">
                          {workload.serving_digest}
                        </span>
                      </Field>
                    </FieldGrid>
                  </Panel>

                  <RolesPanel
                    workload={workload}
                    budgetDevices={placement?.gpu_count}
                  />

                  <ServingBehaviourCard
                    serving={workload.serving.serving}
                    onEdit={() => setEditing("serving")}
                  />

                  <CapabilitiesCard
                    capabilities={workload.serving.capabilities}
                    onEdit={() => setEditing("capabilities")}
                  />
                </div>

                <EngineArgsPanel
                  workload={workload}
                  onEdit={() => setEditing("args")}
                />
              </div>
            </TabsContent>

            {/* ---- Performance -------------------------------------- */}
            <TabsContent value="performance" className="space-y-4">
              <PerformanceView
                workloadId={workload.id}
                placed={workload.deployments.length > 0}
                gpuCount={placement?.gpu_count ?? 0}
              />
            </TabsContent>

            {/* ---- Workbench ---------------------------------------- */}
            <TabsContent value="workbench" className="space-y-4">
              <WorkbenchView
                workload={workload}
                benchmarks={benchmarks}
                curve={curve}
                measured={measured}
                onStartRun={() => setBenchOpen(true)}
              />
            </TabsContent>

            {/* ---- Placement ---------------------------------------- */}
            <TabsContent value="placement" className="space-y-4">
              <PlacementView
                workload={workload}
                placement={placement}
                recentEvents={recentEvents}
                eventsLoading={events.isLoading}
                onEditPlacement={() => setEditing("placement")}
                onSeeAllEvents={() => setView("logs")}
              />
            </TabsContent>

            {/* ---- Lifecycle ---------------------------------------- */}
            <TabsContent value="lifecycle" className="space-y-4">
              <StatRow>
                <StatTile label="Current revision" value={workload.revision} />
                <StatTile
                  label="Started"
                  value={formatRelative(workload.started_at)}
                />
              </StatRow>

              <div className="flex min-w-0 flex-col gap-4">
                <Panel
                  title="Operations"
                  description="Scale, pause and drain — plane-local, each with its own phase"
                >
                  <DataTable
                    columns={operationColumns()}
                    list={operations}
                    rowKey={(operation) => operation.id}
                    empty="Nothing has been asked of this workload."
                  />
                </Panel>

                <Panel
                  title="Revision history"
                  description="Every serving configuration this workload has held, newest first"
                >
                  <WorkloadRevisions workload={workload} />
                </Panel>
              </div>
            </TabsContent>

            {/* ---- Logs --------------------------------------------- */}
            <TabsContent value="logs" className="space-y-4">
              <Panel
                title="Activity"
                description="Everything recorded against this workload, its deployments, its replicas and its weights"
              >
                <ActivityStream
                  events={events.items}
                  loading={events.isLoading}
                  deployments={workload.deployments}
                />
              </Panel>
            </TabsContent>
          </div>
        </Tabs>
      )}
    </div>
  )
}

/* ---- local presentation helpers ------------------------------------- */

function ViewTab({
  value,
  icon: Icon,
  children,
}: React.PropsWithChildren<{
  value: string
  icon: typeof IconLayoutDashboard
}>) {
  return (
    <TabsTrigger value={value}>
      <Icon />
      {children}
    </TabsTrigger>
  )
}

function EditDialog({
  section,
  workload,
  onClose,
}: {
  section: Exclude<EditSection, null>
  workload: Workload
  onClose: () => void
}) {
  const props = {
    workload,
    open: true,
    onOpenChange: (open: boolean) => !open && onClose(),
  }
  switch (section) {
    case "details":
      return <EditDetailsDialog {...props} />
    case "placement":
      return <EditPlacementDialog {...props} />
    case "engine":
      return <EditEngineDialog {...props} />
    case "args":
      return <EditEngineArgsDialog {...props} />
    case "serving":
      return <EditServingDialog {...props} />
    case "capabilities":
      return <EditCapabilitiesDialog {...props} />
  }
}

/** A panel header's way out to the view that carries the rest of the answer. */
function PanelLink({
  onClick,
  children,
}: React.PropsWithChildren<{ onClick: () => void }>) {
  return (
    <button
      type="button"
      className="font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
      onClick={onClick}
    >
      {children}
    </button>
  )
}

/** How many events the overview panels show before deferring to the Logs tab. */
const RECENT_EVENTS = 5
