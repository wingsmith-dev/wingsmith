import { DataTable } from "@/components/layout/data-table"
import { DeleteDialog } from "@/components/layout/destructive"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
import { WriteAction } from "@/components/layout/write-action"
import {
  DrainWorkloadDialog,
  PauseWorkloadDialog,
  ScaleWorkloadDialog,
} from "@/components/demand/workload-actions"
import { ActivityStream } from "@/components/demand/activity-stream"
import { DeploymentTimeline } from "@/components/demand/deployment-timeline"
import { WorkloadDeploymentsTable } from "@/components/demand/workload-deployments-table"
import { Sparkline } from "@/components/metrics/sparkline"
import { formatMetric, sampleOf, valueOf } from "@/modules/metrics/format"
import { useMetrics } from "@/modules/metrics/queries"
import {
  idleGpuHours,
  latencyStages,
  meanUtilization,
  sumCurvePoints,
  sumCurves,
  utilizationWeek,
  type LatencyStage,
  type UtilizationWeek,
} from "@/modules/metrics/rollup"
import type { Metrics } from "@/modules/metrics/types"
import { operationColumns } from "@/components/machinery/operation-columns"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { workloadPhaseTone } from "@/modules/demand/display"
import {
  useDeleteWorkload,
  useEndpoints,
  useRecipe,
  useWorkload,
  useWorkloadPlacement,
} from "@/modules/demand/queries"
import type {
  Endpoint,
  WorkloadDeployment,
  WorkloadPlacementNode,
  WorkloadReplicaPlacement,
} from "@/modules/demand/types"
import { useWorkloadBenchmarks } from "@/modules/benchmarks/queries"
import { benchmarkColumns } from "@/components/benchmarks/benchmark-columns"
import { NewBenchmarkRequestDialog } from "@/components/benchmarks/new-request"
import { useOperations, useWorkloadEvents } from "@/modules/machinery/queries"
import { WorkloadForm } from "../WorkloadForm"
import {
  IconArrowLeft,
  IconChartHistogram,
  IconChartLine,
  IconCpu,
  IconExternalLink,
  IconFlask,
  IconLayoutDashboard,
  IconTerminal2,
  IconTimeline,
} from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"
import { useMemo, useState } from "react"
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
 * gets an id and a phase and shows up under Lifecycle, because "the plane
 * accepted the intent" and "the capacity exists" are different facts and only
 * the second one is what was asked. The Scale card is gone — scaling lives in
 * the Deployments panel header and the actions menu now — but the operation is
 * unchanged.
 *
 * The page is split into six views — overview, performance, workbench,
 * placement, lifecycle, logs. Events, operations, the endpoints routing to the
 * workload, its live placement, its performance across every cluster it landed
 * in and its benchmark run history are read from the plane; tuning analytics,
 * rollout, probes and logs are not yet backed, so those are deterministic mock
 * values, each marked `TODO-API`.
 */
export function WorkloadDetailPage() {
  const { workloadId } = useParams({ from: "/_app/workloads/$workloadId" })
  const { data: workload, isLoading, error } = useWorkload(workloadId)
  const { data: recipe } = useRecipe(workload?.recipe_id ?? "")
  const { data: placement } = useWorkloadPlacement(workloadId)
  const endpoints = useEndpoints({ workload: workload?.id })
  const deploymentIds = useMemo(
    () => (workload?.deployments ?? []).map((d) => d.id),
    [workload]
  )
  const { data: benchmarks } = useWorkloadBenchmarks(deploymentIds)
  const operations = useOperations({ target: workload?.id })
  const settled =
    (workload?.phase === "running" || workload?.phase === "failed") &&
    workload.scale.ready >= workload.scale.desired
  const events = useWorkloadEvents(workload?.id, settled)
  const recentEvents = events.items?.slice(0, RECENT_EVENTS)
  const remove = useDeleteWorkload()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)
  const [scaling, setScaling] = useState(false)
  const [pausing, setPausing] = useState(false)
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

  return (
    <div className="flex flex-col">
      <PageHeader
        title={workload?.name ?? "Workload"}
        description={workload?.id}
        readActions={
          <RefreshButton
            keys={[["workloads"], ["deployments"], ["operations"], ["events"]]}
          />
        }
        actions={
          <div className="flex gap-2">
            {workload && (
              <WriteAction>
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
                  <Button variant="outline" onClick={() => setEditing(true)}>
                    Edit
                  </Button>
                  <Button variant="outline" onClick={() => setDoomed(true)}>
                    Delete
                  </Button>
                </div>
              </WriteAction>
            )}
            <Button asChild variant="outline">
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
      {workload && benchOpen && (
        <NewBenchmarkRequestDialog
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

      {workload && scale && (
        <Tabs value={view} onValueChange={setView} className="flex-col gap-0">
          <TabsList variant="underline" className="px-4">
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
            <ViewTab value="lifecycle" icon={IconTimeline}>
              Lifecycle
            </ViewTab>
            <ViewTab value="logs" icon={IconTerminal2}>
              Logs
            </ViewTab>
          </TabsList>

          <div className="space-y-4 p-4">
            <div className="flex flex-wrap items-center gap-3">
              <StatusLabel tone={workloadPhaseTone(workload.phase)}>
                {workload.phase}
              </StatusLabel>
              {workload.paused && <StatusLabel tone="warn">paused</StatusLabel>}
              <span className="font-mono text-2xs text-muted-foreground/60">
                {workload.kind} · {workload.deployments.length} cluster
                {workload.deployments.length === 1 ? "" : "s"}
              </span>
            </div>

            {/* ---- Overview ------------------------------------------ */}
            <TabsContent value="overview" className="space-y-4">
              <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
                <div className="flex min-w-0 flex-col gap-4">
                  <TrafficPanel
                    workloadId={workload.id}
                    placed={workload.deployments.length > 0}
                  />

                  <Panel
                    title="Deployments"
                    subtitle={`${workload.deployments.length} cluster${
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
                      deployments={workload.deployments}
                      unit={scale.unit}
                      // TODO-API per-cluster requests/sec is data-plane, seeded here
                      serving={(d) =>
                        (d.desired_replicas * 215).toLocaleString()
                      }
                      onDrain={(deployment) => {
                        setDraining(deployment)
                        setDrainOpen(true)
                      }}
                    />
                  </Panel>

                  <Panel
                    title="Endpoints"
                    subtitle="Entrypoints routing traffic to this workload"
                  >
                    <DataTable
                      columns={workloadEndpointColumns}
                      list={endpoints}
                      rowKey={(e) => e.id}
                      empty="No endpoint routes to this workload."
                    />
                  </Panel>
                </div>

                <div className="flex min-w-0 flex-col gap-4">
                  <Panel title="Details">
                    <FieldGrid>
                      <Field label="Status">
                        <StatusLabel tone={workloadPhaseTone(workload.phase)}>
                          {workload.phase}
                        </StatusLabel>
                      </Field>
                      <Field label="Autoscaling">
                        min {scale.min} · max {scale.max}
                      </Field>
                      <Field label="Clusters">
                        {workload.deployments.length === 0 ? (
                          <Dash>{null}</Dash>
                        ) : (
                          workload.deployments
                            .map((d) => d.cluster_name)
                            .join(", ")
                        )}
                      </Field>
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
                      <Field label="Project">{workload.project_id}</Field>
                      <Field label="Created">
                        {formatAbsolute(workload.created_at)}
                      </Field>
                      <Field label="Labels" className="sm:col-span-2">
                        <Labels labels={workload.labels ?? {}} />
                      </Field>
                    </FieldGrid>
                  </Panel>

                  <Panel title="Recipe & artifact">
                    <FieldGrid>
                      <Field label="Recipe">
                        <Link to="/recipes" className="hover:underline">
                          {recipe?.name ?? workload.recipe_id}
                        </Link>
                      </Field>
                      {/* The recipe's name describes the fit; this is what a
                          client puts in the `model` field of a request, and the
                          two are deliberately different strings. */}
                      <Field label="Model ID">
                        <span className="font-mono">
                          {workload.resolved_served_model_name ||
                            recipe?.resolved_served_model_name ||
                            "—"}
                        </span>
                        {workload.served_model_name ? (
                          <span className="ml-2 text-2xs text-muted-foreground/60">
                            set on this workload
                          </span>
                        ) : recipe?.served_model_name ? (
                          <span className="ml-2 text-2xs text-muted-foreground/60">
                            set on the recipe
                          </span>
                        ) : null}
                      </Field>
                      <Field label="Hardware">
                        {recipe
                          ? `${recipe.shape.device_count}× ${recipe.shape.device_class}`
                          : "—"}
                      </Field>
                      <Field label="Engine">
                        {recipe
                          ? `${recipe.engine} ${recipe.engine_version}`
                          : "—"}
                      </Field>
                      <Field label="Quantization">
                        <Dash>
                          {recipe
                            ? [recipe.quantization, recipe.dtype]
                                .filter(Boolean)
                                .join(" · ") || null
                            : null}
                        </Dash>
                      </Field>
                      <Field label="Context length">
                        {recipe?.context_length ?? "—"}
                      </Field>
                      <Field label="Adapters">
                        {workload.adapter_artifact_ids?.length || "none"}
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

            {/* ---- Performance -------------------------------------- */}
            <TabsContent value="performance" className="space-y-4">
              {workload && (
                <PerformanceView
                  workloadId={workload.id}
                  placed={workload.deployments.length > 0}
                  gpuCount={placement?.gpu_count ?? 0}
                />
              )}
            </TabsContent>

            {/* ---- Workbench ---------------------------------------- */}
            <TabsContent value="workbench" className="space-y-4">
              <StatRow>
                <StatTile
                  label="Last run"
                  value={
                    benchmarks?.length
                      ? formatRelative(
                          benchmarks[0].started_at ?? benchmarks[0].created_at
                        )
                      : "—"
                  }
                />
                <StatTile
                  label="Runs recorded"
                  value={String(benchmarks?.length ?? 0)}
                />
                {/* TODO-API sustainable concurrency is a data-plane analytic, not a benchmark field */}
                <StatTile label="Sustainable concurrency" value="72" />
                {/* TODO-API peak throughput is read from a harness summary the plane never normalises */}
                <StatTile label="Peak throughput tested" value="19.3k tok/s" />
                {/* TODO-API tuning recommendation is not computed by the plane */}
                <StatTile
                  label="Recommendation"
                  value="Current config is optimal"
                  small
                />
              </StatRow>

              <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
                <div className="flex min-w-0 flex-col gap-4">
                  <Panel
                    title="Finding the inflection point"
                    subtitle="Throughput and p99 latency across a concurrency sweep, fp8 · awq · 8x H100"
                  >
                    {/* TODO-API concurrency-sweep results not exposed by the plane */}
                    <ChartLegend
                      items={[
                        {
                          tone: "primary",
                          label: "Throughput",
                          value: "3.2k → 19.3k tok/s",
                        },
                        {
                          tone: "danger",
                          label: "p99 latency",
                          value: "220ms → 4.8s",
                        },
                      ]}
                    />
                    <MiniChart
                      lines={[
                        { points: SWEEP_THROUGHPUT, tone: "primary" },
                        { points: SWEEP_LATENCY, tone: "danger" },
                      ]}
                    />
                    <ChartAxis labels={SWEEP_AXIS} />
                    <FieldGrid>
                      <Field label="Inflection point">
                        72 concurrent requests
                      </Field>
                      <Field label="Beyond 72">
                        Throughput gains flatten (+4%) while p99 latency grows
                        5x
                      </Field>
                    </FieldGrid>
                  </Panel>

                  <Panel
                    title="Parameter sweep"
                    subtitle="Serving-engine tuning, fixed at 64 concurrent requests"
                    action={
                      <WriteAction>
                        <Button size="sm" onClick={() => setBenchOpen(true)}>
                          Run benchmark
                        </Button>
                      </WriteAction>
                    }
                  >
                    {/* TODO-API parameter-sweep tuning is not aggregated by the plane */}
                    <DataTable
                      columns={sweepColumns}
                      list={staticList(MOCK_SWEEP)}
                      rowKey={(r) => r.run}
                      empty=""
                    />
                  </Panel>

                  <Panel
                    title="Run history"
                    subtitle="Every benchmark run taken against this workload's deployments"
                  >
                    <DataTable
                      columns={benchmarkColumns({
                        level: "workload",
                        showDeployment: true,
                        showStep: true,
                        showRequest: true,
                      })}
                      list={staticList(benchmarks ?? [])}
                      rowKey={(run) => run.id}
                      empty="No benchmark has been run against this workload."
                    />
                  </Panel>
                </div>

                <div className="flex min-w-0 flex-col gap-4">
                  <Panel title="New run">
                    <p className="text-2xs text-muted-foreground/70">
                      Requests a workload-level measurement of this recipe. The
                      plane expands the ask into one run per placed deployment
                      and grades it against the frozen standard.
                    </p>
                    <WriteAction>
                      <Button
                        size="sm"
                        className="mt-3"
                        onClick={() => setBenchOpen(true)}
                      >
                        Start run
                      </Button>
                    </WriteAction>
                  </Panel>

                  <Panel title="Recommendation">
                    {/* TODO-API tuning recommendations not computed by the plane */}
                    <FieldGrid>
                      <Field label="Recommended config">
                        fp8 · awq · 8x H100 (current)
                      </Field>
                      <Field label="Headroom before re-tuning">
                        ~30% traffic growth
                      </Field>
                      <Field label="Next suggested test">
                        Speculative decoding soak test (run 4)
                      </Field>
                      <Field label="Last run by">perf-eng · Aug 14</Field>
                    </FieldGrid>
                  </Panel>
                </div>
              </div>
            </TabsContent>

            {/* ---- Placement ---------------------------------------- */}
            <TabsContent value="placement" className="space-y-4">
              <StatRow>
                <StatTile
                  label="Nodes"
                  value={placement ? String(placement.node_count) : "—"}
                />
                <StatTile
                  label="GPUs allocated"
                  value={placement ? String(placement.gpu_count) : "—"}
                />
                {/* TODO-API topology packing metrics not exposed at workload scope */}
                <StatTile label="Node packing" value="75%" />
                <StatTile label="Spot instances" value="0%" />
                <StatTile label="Rescheduled (7d)" value="1" />
              </StatRow>

              <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
                <div className="flex min-w-0 flex-col gap-4">
                  <Panel
                    title="Topology"
                    subtitle={`GPU allocation across ${
                      placement?.node_count ?? 0
                    } nodes in ${workload.deployments.length} clusters`}
                  >
                    <div className="flex flex-col gap-3">
                      {(placement?.nodes ?? []).map((node) => (
                        <GpuNodeCard key={node.node} node={node} />
                      ))}
                      {placement && placement.nodes.length === 0 && (
                        <p className="text-2xs text-muted-foreground/60">
                          This workload holds no devices right now.
                        </p>
                      )}
                    </div>
                    <div className="mt-3 flex flex-wrap gap-4 text-2xs text-muted-foreground">
                      <LegendSwatch>This workload</LegendSwatch>
                    </div>
                  </Panel>

                  <Panel
                    title="Replica placement"
                    subtitle="Node and GPU assignment for each live allocation"
                  >
                    <DataTable
                      columns={replicaColumns}
                      list={staticList(placement?.replicas ?? [])}
                      rowKey={(r) => r.allocation}
                      empty="No replicas placed."
                    />
                  </Panel>
                </div>

                <div className="flex min-w-0 flex-col gap-4">
                  <Panel title="Scheduling constraints">
                    <FieldGrid>
                      <Field label="Priority class">
                        {workload.priority_class}
                      </Field>
                      <Field label="Preemptible">
                        {workload.preemptible ? "yes" : "no"}
                      </Field>
                      <Field label="Strategy">{workload.strategy}</Field>
                      <Field label="Fan-out cap">
                        {workload.max_clusters ?? "none"}
                      </Field>
                      {/* TODO-API affinity / anti-affinity / tolerations not modelled by the plane */}
                      <Field label="Node affinity">gpu-type=H100</Field>
                      <Field label="Anti-affinity">
                        Spread across nodes (soft)
                      </Field>
                      <Field label="Placement grouping">
                        NVLink-grouped GPU pairs
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
                          "none"
                        )}
                      </Field>
                    </FieldGrid>
                  </Panel>

                  <Panel title="Capacity">
                    {/* TODO-API pool capacity accounting not exposed at workload scope */}
                    <FieldGrid>
                      <Field label="GPUs in pool">48</Field>
                      <Field label="Allocated to this workload">12</Field>
                      <Field label="Idle on shared nodes">6</Field>
                      <Field label="Spot / on-demand">0% / 100%</Field>
                    </FieldGrid>
                  </Panel>

                  <Panel
                    title="Recent scheduling events"
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

            {/* ---- Lifecycle ---------------------------------------- */}
            <TabsContent value="lifecycle" className="space-y-4">
              <StatRow>
                <StatTile label="Current revision" value={workload.revision} />
                {/* TODO-API rollout strategy is not a declared field on the workload */}
                <StatTile label="Rollout strategy" value="Rolling" small />
                <StatTile
                  label="Started"
                  value={formatRelative(workload.started_at)}
                />
                {/* TODO-API rollback / time-to-healthy history not exposed */}
                <StatTile label="Rollbacks (30d)" value="0" />
                <StatTile label="Time to healthy" value="3m 42s" />
              </StatRow>

              <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
                <div className="flex min-w-0 flex-col gap-4">
                  <Panel
                    title="Operations"
                    subtitle="Scale, pause and drain — plane-local, each with its own phase"
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
                    subtitle="Recipe and configuration changes over time"
                  >
                    {/* TODO-API revision history not exposed — only the current revision is on the workload */}
                    <DataTable
                      columns={revisionColumns}
                      list={staticList(MOCK_REVISIONS)}
                      rowKey={(r) => r.version}
                      empty=""
                    />
                  </Panel>

                  <Panel
                    title="Canary analysis"
                    subtitle="v11 → v12, quantization migration"
                  >
                    {/* TODO-API canary comparison not computed by the plane */}
                    <div className="grid gap-3 sm:grid-cols-2">
                      <CanaryCard
                        label="Baseline · bf16"
                        metrics={[
                          ["Error rate", "0.04%"],
                          ["p99 latency", "340ms"],
                          ["Cost / 1M tok", "$7.85"],
                        ]}
                      />
                      <CanaryCard
                        canary
                        label="Canary · fp8"
                        metrics={[
                          ["Error rate", "0.03%"],
                          ["p99 latency", "305ms"],
                          ["Cost / 1M tok", "$4.20"],
                        ]}
                      />
                    </div>
                  </Panel>
                </div>

                <div className="flex min-w-0 flex-col gap-4">
                  <Panel title="Rollout strategy">
                    {/* TODO-API rollout tuning not modelled by the plane */}
                    <FieldGrid>
                      <Field label="Strategy">Rolling update</Field>
                      <Field label="Max surge">25%</Field>
                      <Field label="Max unavailable">0</Field>
                      <Field label="Min ready seconds">10</Field>
                    </FieldGrid>
                  </Panel>

                  <Panel title="Health checks">
                    {/* TODO-API probe configuration not exposed at workload scope */}
                    <FieldGrid>
                      <Field label="Readiness probe">GET /health · 10s</Field>
                      <Field label="Liveness probe">GET /health · 30s</Field>
                      <Field label="Startup probe">60s · 10s period</Field>
                      <Field label="Failure threshold">3 consecutive</Field>
                    </FieldGrid>
                  </Panel>

                  <Panel title="Rollback safety">
                    {/* TODO-API automatic-rollback policy not modelled by the plane */}
                    <FieldGrid>
                      <Field label="Automatic rollback">Enabled</Field>
                      <Field label="Trigger">Error rate &gt; 1% for 5m</Field>
                      <Field label="Last automatic rollback">
                        <Dash>{null}</Dash>
                      </Field>
                      <Field label="Grace period">10m</Field>
                    </FieldGrid>
                  </Panel>
                </div>
              </div>
            </TabsContent>

            {/* ---- Logs --------------------------------------------- */}
            <TabsContent value="logs" className="space-y-4">
              <Panel
                title="Activity"
                subtitle="Everything recorded against this workload, its deployments, its replicas and its weights"
              >
                <ActivityStream
                  events={events.items}
                  loading={events.isLoading}
                  deployments={workload.deployments}
                />
              </Panel>

              <Panel title="Replica logs" subtitle="Not served from here">
                <p className="text-xs text-muted-foreground">
                  A replica's stdout goes from the cluster to the observability
                  stack directly and never crosses the control plane, so there
                  is nothing here to show and nothing that could be shown
                  without making this service the inference SLA. Read them
                  against the cluster, or in the observability stack. What the
                  plane can answer — why a replica has not started, what it is
                  waiting on, what changed and when — is the stream above.
                </p>
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

function StatRow({ children }: React.PropsWithChildren) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {children}
    </div>
  )
}

function StatTile({
  label,
  value,
  tone,
  small,
}: {
  label: string
  value: string
  tone?: "ok" | "warn"
  small?: boolean
}) {
  return (
    <Card size="sm" className="gap-1.5 px-3">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </div>
      <div
        className={cn(
          "font-mono leading-none tabular-nums",
          small ? "text-sm" : "text-xl",
          tone === "ok" && "text-emerald-600 dark:text-emerald-400",
          tone === "warn" && "text-amber-600 dark:text-amber-400"
        )}
      >
        {value}
      </div>
    </Card>
  )
}

/** How many events the overview panels show before deferring to the Logs tab. */
const RECENT_EVENTS = 5

/**
 * The link out of a capped activity panel and into the full log.
 */
function SeeAllEvents({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      className="font-mono text-2xs text-muted-foreground hover:text-foreground hover:underline"
      onClick={onClick}
    >
      See all
    </button>
  )
}

function Panel({
  title,
  subtitle,
  action,
  children,
}: React.PropsWithChildren<{
  title: string
  subtitle?: string
  action?: React.ReactNode
}>) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-sm">{title}</CardTitle>
            {subtitle && (
              <p className="mt-1 text-2xs text-muted-foreground/60">
                {subtitle}
              </p>
            )}
          </div>
          {action}
        </div>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

/**
 * The Overview traffic chart — how much work is actually arriving.
 *
 * `inference.requests_finished` is a rate the engines count and it sums across
 * clusters, so the curve is every cluster's share added at each step. It counts
 * no request that never reached a cluster, which is why this is a traffic chart
 * and not an availability one — the number that would answer the second lives
 * on the gateway's own series and this console does not read it yet.
 *
 * The range buttons are three different questions, not three shapes of one
 * answer: each re-asks the plane with its own span and step, because an hour
 * read at hourly resolution is one point.
 */
function TrafficPanel({
  workloadId,
  placed,
}: {
  workloadId: string
  placed: boolean
}) {
  const [range, setRange] = useState<TrafficRange>("24h")
  const { span, step } = TRAFFIC_RANGES[range]
  const { data, isLoading, error } = useMetrics(
    "workload",
    placed ? workloadId : undefined,
    { series: true, span, step }
  )
  const points = sumCurvePoints(
    data?.series ?? [],
    "inference.requests_finished"
  )

  return (
    <Panel
      title="Traffic"
      subtitle="Requests the engines completed per second, across every cluster"
      action={<RangeTabs value={range} onChange={setRange} />}
    >
      {!placed ? (
        <EmptyNote>
          this workload runs on no cluster, so no engine is counting requests
          for it
        </EmptyNote>
      ) : isLoading ? (
        <Skeleton className="h-24" />
      ) : error ? (
        <EmptyNote>{formatError(error, "traffic unavailable")}</EmptyNote>
      ) : points.length < 2 ? (
        <EmptyNote>
          {data?.unavailable ??
            `no request rate reported over the last ${range}`}
        </EmptyNote>
      ) : (
        <>
          <ChartLegend
            items={[
              {
                tone: "primary",
                label: "Completed",
                value: formatMetric(
                  points[points.length - 1].value,
                  "per_second"
                ),
              },
            ]}
          />
          <Sparkline points={points} size="lg" tone="primary" />
          <ChartAxis labels={trafficAxis(points)} />
        </>
      )}
    </Panel>
  )
}

/** Each range is its own query: an hour at hourly resolution is one point. */
const TRAFFIC_RANGES = {
  "1h": { span: 3600, step: "1m" },
  "24h": { span: 24 * 3600, step: "5m" },
  "7d": { span: 7 * 24 * 3600, step: "1h" },
} as const

type TrafficRange = keyof typeof TRAFFIC_RANGES

/** First, middle and last stamp of what actually came back, in local time. */
function trafficAxis(points: { at: number }[]): string[] {
  return [0, Math.floor(points.length / 2), points.length - 1].map((i) =>
    new Date(points[i].at * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    })
  )
}

function RangeTabs({
  value,
  onChange,
}: {
  value: TrafficRange
  onChange: (r: TrafficRange) => void
}) {
  return (
    <div className="flex gap-0.5 rounded-md bg-muted p-0.5">
      {(Object.keys(TRAFFIC_RANGES) as TrafficRange[]).map((r) => (
        <button
          key={r}
          type="button"
          onClick={() => onChange(r)}
          className={cn(
            "rounded px-2 py-0.5 font-mono text-2xs",
            value === r
              ? "bg-card text-foreground"
              : "text-muted-foreground hover:text-foreground"
          )}
        >
          {r}
        </button>
      ))}
    </div>
  )
}

const STROKE = {
  primary: "stroke-primary",
  warn: "stroke-amber-500",
  danger: "stroke-destructive",
} as const

const DOT = {
  primary: "bg-primary",
  warn: "bg-amber-500",
  danger: "bg-destructive",
} as const

/**
 * A small grid-backed line chart taking up to two series on a shared scale, so
 * that two measurements read against one axis. Deliberately not the
 * `Sparkline`, which auto-scales each series on its own and would misalign a
 * pair. Pure SVG, no charting dependency.
 */
function MiniChart({
  lines,
}: {
  lines: { points: number[]; tone: keyof typeof STROKE }[]
}) {
  const all = lines.flatMap((l) => l.points)
  const max = Math.max(...all, 1)
  const min = Math.min(...all, 0)
  const span = max - min || 1
  const w = 560
  const h = 130
  const toPoints = (pts: number[]) =>
    pts
      .map(
        (v, i) =>
          `${((i / Math.max(pts.length - 1, 1)) * w).toFixed(1)},${(
            h -
            ((v - min) / span) * (h - 16) -
            8
          ).toFixed(1)}`
      )
      .join(" ")
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      className="h-32 w-full"
      aria-hidden
    >
      {[10, 45, 80, 115].map((y) => (
        <line
          key={y}
          x1={0}
          y1={y}
          x2={w}
          y2={y}
          className="stroke-border"
          strokeWidth={1}
        />
      ))}
      {lines.map((line, i) => (
        <polyline
          key={i}
          points={toPoints(line.points)}
          fill="none"
          strokeWidth={1.75}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
          className={STROKE[line.tone]}
        />
      ))}
    </svg>
  )
}

function ChartLegend({
  items,
}: {
  items: { tone: keyof typeof DOT; label: string; value: string }[]
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-3.5">
      {items.map((item) => (
        <span
          key={item.label}
          className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground"
        >
          <span className={cn("size-2 rounded-full", DOT[item.tone])} />
          {item.label}{" "}
          <span className="font-mono text-foreground">{item.value}</span>
        </span>
      ))}
    </div>
  )
}

function ChartAxis({ labels }: { labels: string[] }) {
  return (
    <div className="mt-1 flex justify-between font-mono text-2xs text-muted-foreground">
      {labels.map((label, i) => (
        <span key={i}>{label}</span>
      ))}
    </div>
  )
}

/* ---- performance ------------------------------------------------------ */

/** A week of hourly buckets, which is what the heatmap draws. */
const HEATMAP_SPAN = 7 * 24 * 3600
const HEATMAP_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

/**
 * How the workload is performing, everywhere placement put it.
 *
 * A workload is the one object that spans clusters, and the plane answers it as
 * its own scope rather than as an average this console computes: throughputs
 * and counts add, the hottest card wins, and a quantile or a mean that cannot
 * cross clusters honestly comes back unmeasured with every cluster's own figure
 * in the breakdown. So nothing here re-derives a merged number — a tile that
 * reads "—" is the plane refusing, and that refusal is information.
 *
 * Two requests, not six panels' worth: one for the live window every tile and
 * the two charts read, and one long, coarse one for the week the heatmap draws.
 */
function PerformanceView({
  workloadId,
  placed,
  gpuCount,
}: {
  workloadId: string
  placed: boolean
  gpuCount: number
}) {
  const live = useMetrics("workload", placed ? workloadId : undefined, {
    series: true,
  })
  const week = useMetrics("workload", placed ? workloadId : undefined, {
    series: true,
    span: HEATMAP_SPAN,
    step: "1h",
  })

  if (!placed) {
    return (
      <MetricsNotice>
        This workload runs on no cluster, so nothing is measuring it.
        Performance appears once placement admits it somewhere.
      </MetricsNotice>
    )
  }
  if (live.isLoading) {
    return (
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {[0, 1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-16" />
        ))}
      </div>
    )
  }
  if (live.error) {
    return (
      <MetricsNotice>
        {formatError(live.error, "metrics unavailable")}
      </MetricsNotice>
    )
  }
  if (!live.data) return null
  /**
   * A workload spans clusters, so `unavailable` and real numbers arrive
   * together: one cluster's store being down is a reason, not an empty page,
   * and the clusters that did answer are still measured. Only an answer with
   * nothing in it at all becomes the notice.
   */
  if (live.data.unavailable && live.data.samples.length === 0) {
    return <MetricsNotice>{live.data.unavailable}</MetricsNotice>
  }

  const data = live.data
  const out = sumCurves(data.series, "inference.output_tokens_per_second")
  const input = sumCurves(data.series, "inference.input_tokens_per_second")
  const week7 = utilizationWeek(week.data)
  const idleHours = idleGpuHours(week.data, gpuCount, HEATMAP_SPAN)

  return (
    <>
      <StatRow>
        <StatTile
          label="GPU utilization"
          value={reading(data, "gpu.utilization")}
        />
        <StatTile
          label="KV cache utilization"
          value={reading(data, "inference.kv_cache_utilization")}
        />
        <StatTile
          label="Requests running"
          value={reading(data, "inference.requests_running")}
        />
        <StatTile
          label="Tokens / sec"
          value={reading(data, "inference.output_tokens_per_second")}
        />
        <StatTile
          label="Requests queued"
          value={reading(data, "inference.requests_waiting")}
        />
      </StatRow>

      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <div className="flex min-w-0 flex-col gap-4">
          <Panel
            title="Throughput"
            subtitle="Input and output tokens per second, summed over every replica"
          >
            {out.length > 1 || input.length > 1 ? (
              <>
                <ChartLegend
                  items={[
                    {
                      tone: "primary",
                      label: "Output",
                      value: reading(
                        data,
                        "inference.output_tokens_per_second"
                      ),
                    },
                    {
                      tone: "warn",
                      label: "Input",
                      value: reading(data, "inference.input_tokens_per_second"),
                    },
                  ]}
                />
                <MiniChart
                  lines={[
                    { points: out, tone: "primary" },
                    { points: input, tone: "warn" },
                  ]}
                />
              </>
            ) : (
              <EmptyNote>
                the metrics store returned no token throughput series for this
                window
              </EmptyNote>
            )}
          </Panel>

          <Panel
            title="Latency breakdown"
            subtitle="Where a request spends its time, from the engines serving it"
          >
            <DataTable
              columns={latencyColumns}
              list={staticList(
                latencyStages(data.samples).map((stage) => ({
                  ...stage,
                  value: reading(data, stage.metric),
                }))
              )}
              rowKey={(r) => r.metric}
              empty="No latency series in this window."
            />
          </Panel>

          <Panel
            title="GPU utilization"
            subtitle="Hourly average across the cards this workload holds, last 7 days"
            action={<HeatmapLegend />}
          >
            {week.isLoading ? (
              <Skeleton className="h-40" />
            ) : week.error ? (
              <EmptyNote>
                {formatError(week.error, "the week could not be read")}
              </EmptyNote>
            ) : week7.length === 0 ? (
              <EmptyNote>
                {week.data?.unavailable ??
                  "no hourly utilization series for the last 7 days"}
              </EmptyNote>
            ) : (
              week7.map((curve) => (
                <GpuHeatmap key={curve.label} curve={curve} />
              ))
            )}
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Panel title="Resource utilization">
            <FieldGrid>
              <Field label="Framebuffer used">
                {reading(data, "gpu.memory_used")}
              </Field>
              <Field label="Framebuffer free">
                {reading(data, "gpu.memory_free")}
              </Field>
              <Field label="Memory bandwidth">
                {reading(data, "gpu.memory_utilization")}
              </Field>
              <Field label="SM clock">{reading(data, "gpu.sm_clock")}</Field>
              <Field label="Power draw">{reading(data, "gpu.power")}</Field>
              <Field label="GPU temperature">
                {reading(data, "gpu.temperature")}
              </Field>
            </FieldGrid>
          </Panel>

          <Panel
            title="Efficiency"
            subtitle="Derived from the utilization series above and the cards placement holds"
          >
            <FieldGrid>
              <Field label="Mean utilization (7d)">
                {formatMetric(meanUtilization(week.data), "percent")}
              </Field>
              <Field label="Idle GPU-hours (7d)">
                {idleHours === null ? (
                  <Dash>{null}</Dash>
                ) : (
                  idleHours.toFixed(1)
                )}
              </Field>
              <Field label="Cards held">
                {gpuCount ? String(gpuCount) : <Dash>{null}</Dash>}
              </Field>
              <Field label="Window">{data.window}</Field>
            </FieldGrid>
          </Panel>

          {data.breakdown.length > 1 && (
            <Panel
              title="Per cluster"
              subtitle="Each cluster's own figures, which is where a number that cannot cross clusters survives"
            >
              <div className="flex flex-col gap-3">
                {data.breakdown.map((member) => (
                  <div key={member.key} className="flex flex-col gap-1">
                    <div className="font-mono text-2xs text-muted-foreground/60">
                      {member.label}
                    </div>
                    <FieldGrid>
                      <Field label="GPU utilization">
                        {formatMetric(
                          valueOf(member.samples, "gpu.utilization"),
                          "percent"
                        )}
                      </Field>
                      <Field label="Tokens / sec">
                        {formatMetric(
                          valueOf(
                            member.samples,
                            "inference.output_tokens_per_second"
                          ),
                          "per_second"
                        )}
                      </Field>
                    </FieldGrid>
                  </div>
                ))}
              </div>
            </Panel>
          )}
        </div>
      </div>

      {data.unavailable && (
        <div className="text-2xs text-muted-foreground/60">
          Partly measured: {data.unavailable}
        </div>
      )}

      {data.source && (
        <div className="font-mono text-2xs text-muted-foreground/50">
          {data.window} window · {data.source.kind} · {data.source.url}
        </div>
      )}
    </>
  )
}

/** One metric as the console spells it, or an em dash where nothing measured it. */
function reading(data: Metrics, metric: string): string {
  const sample = sampleOf(data.samples, metric)
  return sample ? formatMetric(sample.value, sample.unit) : "—"
}

function MetricsNotice({ children }: React.PropsWithChildren) {
  return (
    <Card size="sm" className="items-start gap-1 px-3 py-3">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <IconChartLine className="size-4 shrink-0 opacity-60" />
        {children}
      </div>
    </Card>
  )
}

function EmptyNote({ children }: React.PropsWithChildren) {
  return (
    <p className="py-6 text-center text-2xs text-muted-foreground/60">
      {children}
    </p>
  )
}

/** A stage the store answered, with its figure already rendered. */
type LatencyRow = LatencyStage & { value: string }

const latencyColumns = [
  {
    key: "stage",
    header: "Stage",
    cell: (r: LatencyStage) => <span className="text-xs">{r.stage}</span>,
  },
  {
    key: "metric",
    header: "Metric",
    secondary: true,
    cell: (r: LatencyStage) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {r.metric}
      </span>
    ),
  },
  {
    key: "value",
    header: "Value",
    align: "right" as const,
    cell: (r: LatencyRow) => mono(r.value),
  },
]

function GpuHeatmap({ curve }: { curve: UtilizationWeek }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="font-mono text-2xs text-muted-foreground/60">
        {curve.label}
      </div>
      {HEATMAP_DAYS.map((label, day) => (
        <div key={label} className="flex items-center gap-2.5">
          <span className="w-8 shrink-0 font-mono text-2xs text-muted-foreground">
            {label}
          </span>
          <div className="grid flex-1 grid-cols-[repeat(24,minmax(0,1fr))] gap-0.5">
            {Array.from({ length: 24 }, (_, hour) => {
              const pct = curve.grid[day][hour]
              return (
                <span
                  key={hour}
                  title={
                    pct === null
                      ? `${label} ${String(hour).padStart(2, "0")}:00 — not reported`
                      : `${label} ${String(hour).padStart(2, "0")}:00 — ${Math.round(pct)}% GPU util`
                  }
                  className={cn(
                    "aspect-square rounded-[2px]",
                    pct === null && "border border-dashed border-border"
                  )}
                  style={
                    pct === null
                      ? undefined
                      : {
                          backgroundColor: `color-mix(in oklab, var(--primary) ${Math.round(
                            Math.min(100, Math.max(0, pct)) * 0.75
                          )}%, var(--secondary))`,
                        }
                  }
                />
              )
            })}
          </div>
        </div>
      ))}
      <div className="flex justify-between pl-[2.625rem] font-mono text-2xs text-muted-foreground">
        <span>00:00</span>
        <span>06:00</span>
        <span>12:00</span>
        <span>18:00</span>
        <span>23:00</span>
      </div>
    </div>
  )
}

function HeatmapLegend() {
  return (
    <div className="flex items-center gap-1.5 text-2xs text-muted-foreground">
      Low
      <span className="flex gap-0.5">
        {[12, 32, 52, 72, 92].map((pct) => (
          <span
            key={pct}
            className="size-2.5 rounded-[2px]"
            style={{
              backgroundColor: `color-mix(in oklab, var(--primary) ${pct}%, var(--secondary))`,
            }}
          />
        ))}
      </span>
      High
    </div>
  )
}

/* ---- placement topology --------------------------------------------- */

/**
 * The workload's own occupancy of one node, from `/placement`. The plane
 * reports how many of the node's devices this workload holds; it does not
 * report the node's total capacity or what other workloads hold, so the card
 * draws the held devices and no idle or foreign slots.
 */
function slotStyle(): React.CSSProperties {
  return {
    backgroundColor: "color-mix(in oklab, var(--primary) 55%, transparent)",
    borderColor: "var(--primary)",
  }
}

function GpuNodeCard({ node }: { node: WorkloadPlacementNode }) {
  return (
    <div className="flex flex-col gap-2.5 rounded-md border bg-muted/40 px-3.5 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-xs font-semibold">{node.node}</span>
        <span className="font-mono text-2xs text-muted-foreground">
          {node.cluster} · {node.device_count} GPU
          {node.device_count === 1 ? "" : "s"} held
        </span>
      </div>
      <div className="grid grid-cols-8 gap-1">
        {Array.from({ length: node.device_count }, (_, i) => (
          <span
            key={i}
            className="aspect-square rounded border"
            style={slotStyle()}
          />
        ))}
      </div>
    </div>
  )
}

function LegendSwatch({ children }: React.PropsWithChildren) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block size-2.5 rounded-[2px] border"
        style={slotStyle()}
      />
      {children}
    </span>
  )
}

/* ---- canary comparison ---------------------------------------------- */

function CanaryCard({
  label,
  metrics,
  canary,
}: {
  label: string
  metrics: [string, string][]
  canary?: boolean
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-md border px-3.5 py-3",
        canary && "border-primary/35 bg-primary/5"
      )}
    >
      <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        {label}
      </span>
      {metrics.map(([k, v]) => (
        <div key={k} className="flex items-baseline justify-between text-xs">
          <span>{k}</span>
          <span className="font-mono">{v}</span>
        </div>
      ))}
    </div>
  )
}

/**
 * A `ListState` wrapper for rows already in hand — a derived table, or one of
 * the designed-but-unbacked ones — so they render through the same `DataTable`
 * as the paged, live queries.
 */
function staticList<T>(items: T[]) {
  return {
    items,
    loaded: items.length,
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: () => {},
  }
}

/* ---- mocked series (all TODO-API, see call sites) ------------------- */

const SWEEP_THROUGHPUT = [3200, 6800, 10400, 13600, 16200, 18420, 19000, 19300]
const SWEEP_LATENCY = [220, 240, 280, 340, 420, 890, 2600, 4800]
const SWEEP_AXIS = ["8", "16", "32", "48", "64", "72", "96", "128"]

/* ---- mocked columns and rows (all TODO-API, see call sites) ---------- */

const mono = (v: string) => <span className="font-mono text-xs">{v}</span>

const workloadEndpointColumns = [
  {
    key: "name",
    header: "Endpoint",
    cell: (e: Endpoint) => (
      <Link
        to="/endpoints/$endpointId"
        params={{ endpointId: e.id }}
        className="inline-flex items-center gap-1 hover:underline"
      >
        <span className="font-mono text-xs">{e.name}</span>
        <IconExternalLink className="size-3 text-muted-foreground" />
      </Link>
    ),
  },
  {
    key: "status",
    header: "Status",
    cell: (e: Endpoint) => (
      <span className="inline-flex items-center gap-2">
        <StatusDot tone={e.enabled ? "ok" : "neutral"} />
        <span className="text-2xs text-muted-foreground">
          {e.enabled ? "enabled" : "disabled"}
        </span>
      </span>
    ),
  },
  {
    key: "visibility",
    header: "Visibility",
    cell: (e: Endpoint) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {e.visibility}
      </span>
    ),
  },
  {
    key: "auth",
    header: "Auth",
    cell: (e: Endpoint) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {e.surface?.auth_mode ?? "—"}
      </span>
    ),
  },
  {
    key: "updated",
    header: "Updated",
    align: "right" as const,
    cell: (e: Endpoint) => mono(formatRelative(e.updated_at ?? e.created_at)),
  },
]

interface SweepRow {
  run: string
  seqs: string
  batch: string
  kv: string
  spec: string
  throughput: string
  p99: string
  verdict: string
}
const MOCK_SWEEP: SweepRow[] = [
  {
    run: "1",
    seqs: "32",
    batch: "4096",
    kv: "16",
    spec: "Off",
    throughput: "12,100 tok/s",
    p99: "340ms",
    verdict: "Baseline",
  },
  {
    run: "2",
    seqs: "64",
    batch: "8192",
    kv: "16",
    spec: "Off",
    throughput: "16,900 tok/s",
    p99: "420ms",
    verdict: "Better",
  },
  {
    run: "3",
    seqs: "64",
    batch: "8192",
    kv: "32",
    spec: "Off",
    throughput: "18,420 tok/s",
    p99: "890ms",
    verdict: "Current",
  },
  {
    run: "4",
    seqs: "64",
    batch: "8192",
    kv: "32",
    spec: "On · draft 8b",
    throughput: "21,600 tok/s",
    p99: "610ms",
    verdict: "Needs soak",
  },
]
const sweepColumns = [
  { key: "run", header: "Run", cell: (r: SweepRow) => mono(r.run) },
  {
    key: "seqs",
    header: "max_num_seqs",
    align: "right" as const,
    cell: (r: SweepRow) => mono(r.seqs),
  },
  {
    key: "batch",
    header: "max_batch_tokens",
    align: "right" as const,
    cell: (r: SweepRow) => mono(r.batch),
  },
  {
    key: "kv",
    header: "KV block",
    align: "right" as const,
    cell: (r: SweepRow) => mono(r.kv),
  },
  {
    key: "spec",
    header: "Spec. decoding",
    cell: (r: SweepRow) => <span className="text-xs">{r.spec}</span>,
  },
  {
    key: "throughput",
    header: "Throughput",
    align: "right" as const,
    cell: (r: SweepRow) => mono(r.throughput),
  },
  {
    key: "p99",
    header: "p99 latency",
    align: "right" as const,
    cell: (r: SweepRow) => mono(r.p99),
  },
  {
    key: "verdict",
    header: "Verdict",
    cell: (r: SweepRow) => <span className="text-xs">{r.verdict}</span>,
  },
]

const replicaColumns = [
  {
    key: "replica",
    header: "Replica",
    cell: (r: WorkloadReplicaPlacement) =>
      r.replica ? (
        mono(r.replica)
      ) : (
        <span className="font-mono text-2xs text-muted-foreground/60">
          held (no replica yet)
        </span>
      ),
  },
  {
    key: "allocation",
    header: "Allocation",
    secondary: true,
    cell: (r: WorkloadReplicaPlacement) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {r.allocation}
      </span>
    ),
  },
  {
    key: "cluster",
    header: "Cluster",
    cell: (r: WorkloadReplicaPlacement) => (
      <span className="text-xs">{r.cluster}</span>
    ),
  },
  {
    key: "nodes",
    header: "Nodes",
    cell: (r: WorkloadReplicaPlacement) => (
      <span className="font-mono text-2xs">
        {r.nodes.length ? r.nodes.join(", ") : "—"}
      </span>
    ),
  },
  {
    key: "gpus",
    header: "GPUs",
    align: "right" as const,
    cell: (r: WorkloadReplicaPlacement) => mono(String(r.device_count)),
  },
  {
    key: "status",
    header: "Gang",
    cell: (r: WorkloadReplicaPlacement) => (
      <span className="inline-flex items-center gap-2">
        <StatusDot tone={r.gang_complete ? "ok" : "warn"} />
        <span className="text-2xs text-muted-foreground">
          {r.gang_complete ? "complete" : "partial"}
        </span>
      </span>
    ),
  },
]

interface RevisionRow {
  version: string
  change: string
  rollout: string
  status: string
  statusTone: "ok" | "neutral"
  deployed: string
}
const MOCK_REVISIONS: RevisionRow[] = [
  {
    version: "v12",
    change: "llama-3.1-70b-chat-fp8 (recipe updated)",
    rollout: "Rolling",
    status: "Active",
    statusTone: "ok",
    deployed: "Aug 14",
  },
  {
    version: "v11",
    change: "Quantization bf16 → fp8",
    rollout: "Canary (10% → 100%)",
    status: "Superseded",
    statusTone: "neutral",
    deployed: "Aug 09",
  },
  {
    version: "v10",
    change: "Hardware 8x A100 → 8x H100",
    rollout: "Rolling",
    status: "Superseded",
    statusTone: "neutral",
    deployed: "Jul 30",
  },
  {
    version: "v9",
    change: "Initial release (bf16, 8x A100)",
    rollout: "Rolling",
    status: "Superseded",
    statusTone: "neutral",
    deployed: "Jul 28",
  },
]
const revisionColumns = [
  {
    key: "version",
    header: "Version",
    cell: (r: RevisionRow) => (
      <span
        className={cn(
          "rounded-full px-1.5 py-0.5 font-mono text-2xs",
          r.status === "Active"
            ? "bg-primary/15 text-primary"
            : "bg-muted text-muted-foreground"
        )}
      >
        {r.version}
      </span>
    ),
  },
  {
    key: "change",
    header: "Change",
    cell: (r: RevisionRow) => <span className="text-xs">{r.change}</span>,
  },
  {
    key: "rollout",
    header: "Rollout",
    cell: (r: RevisionRow) => <span className="text-xs">{r.rollout}</span>,
  },
  {
    key: "status",
    header: "Status",
    cell: (r: RevisionRow) => (
      <span className="inline-flex items-center gap-2">
        <StatusDot tone={r.statusTone} />
        <span className="text-2xs text-muted-foreground">{r.status}</span>
      </span>
    ),
  },
  {
    key: "deployed",
    header: "Deployed",
    align: "right" as const,
    cell: (r: RevisionRow) => mono(r.deployed),
  },
]
