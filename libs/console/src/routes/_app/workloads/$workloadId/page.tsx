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
 * workload, its live placement and its benchmark run history are read from the
 * plane; serving metrics, tuning analytics, rollout, probes and logs are
 * data-plane or not yet backed, so those are deterministic mock values, each
 * marked `TODO-API`.
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
                <div className="flex flex-col gap-4">
                  <TrafficPanel />

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

                <div className="flex flex-col gap-4">
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

                  <Panel title="Activity">
                    <DeploymentTimeline
                      events={events.items}
                      loading={events.isLoading}
                    />
                  </Panel>
                </div>
              </div>
            </TabsContent>

            {/* ---- Performance -------------------------------------- */}
            <TabsContent value="performance" className="space-y-4">
              {/* TODO-API workload-level performance rollups — metrics are per-deployment only */}
              <StatRow>
                <StatTile label="GPU utilization" value="78%" />
                <StatTile label="KV cache utilization" value="64%" />
                <StatTile label="Avg batch size" value="24.3" />
                <StatTile label="Tokens / sec" value="18,420" />
                <StatTile label="Queue depth (p50)" value="6" />
              </StatRow>

              <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
                <div className="flex flex-col gap-4">
                  <Panel
                    title="Throughput"
                    subtitle="Input and output tokens per second"
                  >
                    {/* TODO-API token throughput series not exposed at workload scope */}
                    <ChartLegend
                      items={[
                        {
                          tone: "primary",
                          label: "Output",
                          value: "12,180 tok/s",
                        },
                        {
                          tone: "warn",
                          label: "Input",
                          value: "6,240 tok/s",
                        },
                      ]}
                    />
                    <MiniChart
                      lines={[
                        { points: THROUGHPUT_OUT, tone: "primary" },
                        { points: THROUGHPUT_IN, tone: "warn" },
                      ]}
                    />
                  </Panel>

                  <Panel
                    title="Latency breakdown"
                    subtitle="Time to first token, inter-token latency, end-to-end"
                  >
                    {/* TODO-API latency-percentile breakdown not exposed by the plane */}
                    <DataTable
                      columns={latencyColumns}
                      list={mockList(MOCK_LATENCY)}
                      rowKey={(r) => r.percentile}
                      empty=""
                    />
                  </Panel>

                  <Panel
                    title="GPU utilization"
                    subtitle="Hourly average across all replicas, last 7 days"
                    action={<HeatmapLegend />}
                  >
                    {/* TODO-API per-hour GPU utilization heatmap not exposed by the plane */}
                    <GpuHeatmap />
                  </Panel>
                </div>

                <div className="flex flex-col gap-4">
                  <Panel title="Resource utilization">
                    {/* TODO-API host CPU / memory / network rollups at workload scope */}
                    <FieldGrid>
                      <Field label="GPU memory (avg)">71% · 681 / 960 GB</Field>
                      <Field label="Host CPU">34%</Field>
                      <Field label="Host memory">58%</Field>
                      <Field label="Network throughput">420 Mbps</Field>
                    </FieldGrid>
                  </Panel>

                  <Panel title="Efficiency">
                    {/* TODO-API efficiency scoring and cost suggestions not computed by the plane */}
                    <FieldGrid>
                      <Field label="Utilization efficiency">78%</Field>
                      <Field label="Idle GPU-hours (24h)">11.4</Field>
                      <Field label="Suggestion" className="sm:col-span-2">
                        Scale us-east-1b to 1 replica during low-traffic windows
                      </Field>
                    </FieldGrid>
                  </Panel>
                </div>
              </div>
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
                <div className="flex flex-col gap-4">
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
                      list={mockList(MOCK_SWEEP)}
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
                      list={mockList(benchmarks ?? [])}
                      rowKey={(run) => run.id}
                      empty="No benchmark has been run against this workload."
                    />
                  </Panel>
                </div>

                <div className="flex flex-col gap-4">
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
                <div className="flex flex-col gap-4">
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
                      list={mockList(placement?.replicas ?? [])}
                      rowKey={(r) => r.allocation}
                      empty="No replicas placed."
                    />
                  </Panel>
                </div>

                <div className="flex flex-col gap-4">
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

                  <Panel title="Recent scheduling events">
                    <DeploymentTimeline
                      events={events.items}
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
                <div className="flex flex-col gap-4">
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
                      list={mockList(MOCK_REVISIONS)}
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

                <div className="flex flex-col gap-4">
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
 * The Overview traffic chart — requests/sec across all deployments, drawn as a
 * filled area with the console's own `Sparkline`. Serving rate is data-plane
 * and never rolled up to the workload, so the series is a seeded mock and the
 * range tabs re-shape it rather than re-fetch.
 */
function TrafficPanel() {
  const [range, setRange] = useState<keyof typeof TRAFFIC>("24h")
  return (
    <Panel
      title="Traffic"
      subtitle="Requests per second, across all deployments"
      action={<RangeTabs value={range} onChange={setRange} />}
    >
      {/* TODO-API workload traffic series — no serving metric rolled up to the workload */}
      <Sparkline points={TRAFFIC[range]} size="lg" tone="primary" />
    </Panel>
  )
}

function RangeTabs({
  value,
  onChange,
}: {
  value: keyof typeof TRAFFIC
  onChange: (r: keyof typeof TRAFFIC) => void
}) {
  return (
    <div className="flex gap-0.5 rounded-md bg-muted p-0.5">
      {(["1h", "24h", "7d"] as const).map((r) => (
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

/* ---- GPU utilization heatmap ---------------------------------------- */

const HEATMAP_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

/** Deterministic hourly GPU utilization, peaking midday and softer on weekends. */
function gpuUtil(day: number, hour: number): number {
  const midday = Math.exp(-Math.pow(hour - 14, 2) / 42)
  const weekend = day >= 5 ? 0.7 : 1
  return Math.round((0.12 + 0.78 * midday * weekend) * 100)
}

function GpuHeatmap() {
  return (
    <div className="flex flex-col gap-1">
      {HEATMAP_DAYS.map((label, day) => (
        <div key={label} className="flex items-center gap-2.5">
          <span className="w-8 shrink-0 font-mono text-2xs text-muted-foreground">
            {label}
          </span>
          <div className="grid flex-1 grid-cols-[repeat(24,minmax(0,1fr))] gap-0.5">
            {Array.from({ length: 24 }, (_, hour) => {
              const pct = gpuUtil(day, hour)
              return (
                <span
                  key={hour}
                  title={`${label} ${String(hour).padStart(2, "0")}:00 — ${pct}% GPU util`}
                  className="aspect-square rounded-[2px]"
                  style={{
                    backgroundColor: `color-mix(in oklab, var(--primary) ${Math.round(
                      pct * 0.75
                    )}%, var(--secondary))`,
                  }}
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
 * A `ListState` wrapper for statically-mocked rows, so designed-but-unbacked
 * tables render through the same `DataTable` as the live ones.
 */
function mockList<T>(items: T[]) {
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

const TRAFFIC = {
  "1h": [
    980, 1010, 1005, 1040, 1080, 1120, 1090, 1140, 1180, 1210, 1195, 1240,
  ].map((value, at) => ({ at, value })),
  "24h": [
    420, 560, 610, 720, 940, 1050, 980, 1180, 1120, 1360, 1300, 1460, 1420,
    1560, 1600,
  ].map((value, at) => ({ at, value })),
  "7d": [
    900, 1200, 1450, 1100, 1350, 700, 640, 1280, 1500, 1320, 1480, 760, 700,
    1240,
  ].map((value, at) => ({ at, value })),
} as const

const THROUGHPUT_OUT = [
  4200, 5600, 6800, 8200, 9400, 10200, 9800, 11800, 11200, 13200, 12600, 14600,
  14200, 15600, 16200,
]
const THROUGHPUT_IN = [
  2100, 2600, 3100, 3800, 4400, 4900, 4600, 5400, 5200, 5900, 5700, 6200, 6100,
  6400, 6600,
]

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

interface LatencyRow {
  percentile: string
  ttft: string
  itl: string
  e2e: string
}
const MOCK_LATENCY: LatencyRow[] = [
  { percentile: "p50", ttft: "210ms", itl: "18ms", e2e: "1.4s" },
  { percentile: "p95", ttft: "480ms", itl: "34ms", e2e: "3.1s" },
  { percentile: "p99", ttft: "890ms", itl: "52ms", e2e: "4.6s" },
]
const latencyColumns = [
  {
    key: "percentile",
    header: "Percentile",
    cell: (r: LatencyRow) => mono(r.percentile),
  },
  {
    key: "ttft",
    header: "TTFT",
    align: "right" as const,
    cell: (r: LatencyRow) => mono(r.ttft),
  },
  {
    key: "itl",
    header: "Inter-token",
    align: "right" as const,
    cell: (r: LatencyRow) => mono(r.itl),
  },
  {
    key: "e2e",
    header: "End-to-end",
    align: "right" as const,
    cell: (r: LatencyRow) => mono(r.e2e),
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
