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
  useRecipe,
  useWorkload,
} from "@/modules/demand/queries"
import type { WorkloadDeployment } from "@/modules/demand/types"
import { useEvents, useOperations } from "@/modules/machinery/queries"
import { WorkloadForm } from "../WorkloadForm"
import {
  IconArrowLeft,
  IconChartHistogram,
  IconCpu,
  IconDownload,
  IconExternalLink,
  IconFlask,
  IconLayoutDashboard,
  IconSearch,
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
 * placement, lifecycle, logs. Events and operations are measured live; serving,
 * topology, burn-in, rollout and log surfaces are data-plane or not yet backed,
 * so they are drawn from deterministic mock values, each marked `TODO-API`.
 */
export function WorkloadDetailPage() {
  const { workloadId } = useParams({ from: "/_app/workloads/$workloadId" })
  const { data: workload, isLoading, error } = useWorkload(workloadId)
  const { data: recipe } = useRecipe(workload?.recipe_id ?? "")
  const operations = useOperations({ target: workload?.id })
  const events = useEvents({ subject: workload?.id })
  const remove = useDeleteWorkload()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)
  const [scaling, setScaling] = useState(false)
  const [pausing, setPausing] = useState(false)
  const [draining, setDraining] = useState<WorkloadDeployment | undefined>()
  const [drainOpen, setDrainOpen] = useState(false)
  const [view, setView] = useState("overview")

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
            </div>

            <Tabs value={view} onValueChange={setView}>
              <TabsList className="h-auto w-full justify-start gap-5 rounded-none border-b bg-transparent p-0">
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

              {/* ---- Overview ------------------------------------------ */}
              <TabsContent value="overview" className="mt-4 space-y-4">
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
                      {/* TODO-API endpoints targeting a workload — no reverse index from workload to endpoint */}
                      <DataTable
                        columns={endpointColumns}
                        list={mockList(MOCK_ENDPOINTS)}
                        rowKey={(e) => e.name}
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
              <TabsContent value="performance" className="mt-4 space-y-4">
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
                        <Field label="GPU memory (avg)">
                          71% · 681 / 960 GB
                        </Field>
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
                          Scale us-east-1b to 1 replica during low-traffic
                          windows
                        </Field>
                      </FieldGrid>
                    </Panel>
                  </div>
                </div>
              </TabsContent>

              {/* ---- Workbench ---------------------------------------- */}
              <TabsContent value="workbench" className="mt-4 space-y-4">
                {/* TODO-API benchmark / burn-in results not exposed by the plane */}
                <StatRow>
                  <StatTile label="Last run" value="2d ago" />
                  <StatTile label="Configs tested" value="4" />
                  <StatTile label="Sustainable concurrency" value="72" />
                  <StatTile label="Peak throughput tested" value="19.3k tok/s" />
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
                          <Button size="sm" disabled>
                            Run benchmark
                          </Button>
                        </WriteAction>
                      }
                    >
                      {/* TODO-API parameter-sweep runs not exposed by the plane */}
                      <DataTable
                        columns={sweepColumns}
                        list={mockList(MOCK_SWEEP)}
                        rowKey={(r) => r.run}
                        empty=""
                      />
                    </Panel>

                    <Panel
                      title="Run history"
                      subtitle="Full configuration comparisons from burn-in testing"
                    >
                      {/* TODO-API benchmark run history not exposed by the plane */}
                      <DataTable
                        columns={runColumns}
                        list={mockList(MOCK_RUNS)}
                        rowKey={(r) => r.config}
                        empty=""
                      />
                    </Panel>
                  </div>

                  <div className="flex flex-col gap-4">
                    <Panel title="New run">
                      {/* TODO-API benchmark request shape not wired to a workload */}
                      <FieldGrid>
                        <Field label="Dataset">ShareGPT · 500 samples</Field>
                        <Field label="Input / output tokens">256 / 256</Field>
                        <Field label="Concurrency sweep">
                          8 → 128, doubling
                        </Field>
                        <Field label="Duration / step">2 min</Field>
                        <Field label="Target SLA">p99 &lt; 1s</Field>
                      </FieldGrid>
                      <WriteAction>
                        <Button size="sm" disabled className="mt-3">
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
              <TabsContent value="placement" className="mt-4 space-y-4">
                {/* TODO-API topology packing metrics not exposed at workload scope */}
                <StatRow>
                  <StatTile label="Nodes" value={String(MOCK_NODES.length)} />
                  <StatTile
                    label="GPUs allocated"
                    value={
                      recipe
                        ? String(recipe.shape.device_count * scale.placed)
                        : "12"
                    }
                  />
                  <StatTile label="Node packing" value="75%" />
                  <StatTile label="Spot instances" value="0%" />
                  <StatTile label="Rescheduled (7d)" value="1" />
                </StatRow>

                <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
                  <div className="flex flex-col gap-4">
                    <Panel
                      title="Topology"
                      subtitle={`GPU allocation across ${MOCK_NODES.length} nodes in ${workload.deployments.length} clusters`}
                    >
                      {/* TODO-API per-node GPU slot occupancy not exposed by the plane */}
                      <div className="flex flex-col gap-3">
                        {MOCK_NODES.map((node) => (
                          <GpuNodeCard key={node.name} node={node} />
                        ))}
                      </div>
                      <div className="mt-3 flex flex-wrap gap-4 text-2xs text-muted-foreground">
                        <LegendSwatch kind="mine">This workload</LegendSwatch>
                        <LegendSwatch kind="other">Other workloads</LegendSwatch>
                        <LegendSwatch kind="idle">Idle</LegendSwatch>
                      </div>
                    </Panel>

                    <Panel
                      title="Replica placement"
                      subtitle="Node and GPU assignment for each replica"
                    >
                      {/* TODO-API per-replica node/GPU assignment not exposed by the plane */}
                      <DataTable
                        columns={replicaColumns}
                        list={mockList(MOCK_REPLICAS)}
                        rowKey={(r) => r.replica}
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
              <TabsContent value="lifecycle" className="mt-4 space-y-4">
                <StatRow>
                  <StatTile
                    label="Current revision"
                    value={workload.revision}
                  />
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
              <TabsContent value="logs" className="mt-4 space-y-4">
                <Panel
                  title="Replica logs"
                  subtitle="Logs stream direct from the cluster and never cross the control plane"
                >
                  {/* TODO-API log streaming is a data-plane concern, not served by the plane */}
                  <LogViewer />
                </Panel>

                <Panel
                  title="Events"
                  subtitle="Scheduler and lifecycle events for this workload"
                >
                  <DeploymentTimeline
                    events={events.items}
                    loading={events.isLoading}
                  />
                </Panel>
              </TabsContent>
            </Tabs>
          </>
        )}
      </div>
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
    <TabsTrigger
      value={value}
      className="gap-1.5 rounded-none border-b-2 border-transparent bg-transparent px-0.5 py-3 text-muted-foreground shadow-none data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none"
    >
      <Icon className="size-4" />
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

type SlotKind = "mine" | "other" | "idle"
interface TopoNode {
  name: string
  meta: string
  slots: SlotKind[]
}
const MOCK_NODES: TopoNode[] = [
  {
    name: "node-0417",
    meta: "us-east-1a · 8x H100 80GB · NVLink",
    slots: ["mine", "mine", "mine", "mine", "other", "other", "other", "other"],
  },
  {
    name: "node-0512",
    meta: "us-east-1a · 8x H100 80GB · NVLink",
    slots: ["mine", "mine", "mine", "mine", "other", "other", "idle", "idle"],
  },
  {
    name: "node-0233",
    meta: "us-east-1b · 8x H100 80GB · NVLink",
    slots: ["mine", "mine", "mine", "mine", "idle", "idle", "idle", "idle"],
  },
]

function slotStyle(kind: SlotKind): React.CSSProperties {
  if (kind === "mine")
    return {
      backgroundColor: "color-mix(in oklab, var(--primary) 55%, transparent)",
      borderColor: "var(--primary)",
    }
  if (kind === "other")
    return {
      backgroundColor:
        "color-mix(in oklab, var(--muted-foreground) 35%, transparent)",
      borderColor: "transparent",
    }
  return { backgroundColor: "transparent" }
}

function GpuNodeCard({ node }: { node: TopoNode }) {
  return (
    <div className="flex flex-col gap-2.5 rounded-md border bg-muted/40 px-3.5 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-xs font-semibold">{node.name}</span>
        <span className="font-mono text-2xs text-muted-foreground">
          {node.meta}
        </span>
      </div>
      <div className="grid grid-cols-8 gap-1">
        {node.slots.map((slot, i) => (
          <span
            key={i}
            className="aspect-square rounded border"
            style={slotStyle(slot)}
          />
        ))}
      </div>
    </div>
  )
}

function LegendSwatch({
  kind,
  children,
}: React.PropsWithChildren<{ kind: SlotKind }>) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block size-2.5 rounded-[2px] border"
        style={slotStyle(kind)}
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

/* ---- logs viewer ---------------------------------------------------- */

/**
 * The Logs surface with its toolbar: replica picker, level filter, search, a
 * live-tail toggle and a download button. Filtering is client-side over the
 * mocked lines — real streaming is data-plane and never crosses the control
 * plane, so the wiring is the shape, not a socket.
 */
function LogViewer() {
  const [replica, setReplica] = useState("all")
  const [level, setLevel] = useState<"all" | "info" | "warn" | "error">("all")
  const [term, setTerm] = useState("")
  const [live, setLive] = useState(false)

  const replicas = useMemo(
    () => Array.from(new Set(MOCK_LOGS.map((l) => l.replica))),
    []
  )

  const rows = MOCK_LOGS.filter((line) => {
    const matchesLevel = level === "all" || line.level.toLowerCase() === level
    const matchesReplica = replica === "all" || line.replica === replica
    const matchesSearch =
      term.trim() === "" ||
      line.msg.toLowerCase().includes(term.trim().toLowerCase())
    return matchesLevel && matchesReplica && matchesSearch
  })

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2.5">
        <select
          value={replica}
          onChange={(e) => setReplica(e.target.value)}
          className="h-8 rounded-md border border-input bg-card px-2 font-mono text-2xs"
        >
          <option value="all">All replicas</option>
          {replicas.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>

        <div className="flex gap-0.5 rounded-md bg-muted p-0.5">
          {(["all", "info", "warn", "error"] as const).map((l) => (
            <button
              key={l}
              type="button"
              onClick={() => setLevel(l)}
              className={cn(
                "rounded px-2.5 py-1 text-xs capitalize",
                level === l
                  ? "bg-card text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              {l}
            </button>
          ))}
        </div>

        <label className="relative flex items-center">
          <IconSearch className="pointer-events-none absolute left-2 size-3.5 text-muted-foreground" />
          <input
            type="search"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="Search logs…"
            className="h-8 w-52 rounded-md border border-input bg-card pl-7 pr-3 text-xs placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
        </label>

        <span className="ml-auto" />

        <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
          Live tail
          <input
            type="checkbox"
            checked={live}
            onChange={(e) => setLive(e.target.checked)}
            className="size-4 accent-primary"
          />
        </label>

        <WriteAction>
          <Button size="sm" variant="outline" disabled>
            <IconDownload className="size-3.5" />
            Download
          </Button>
        </WriteAction>
      </div>

      <div className="h-[360px] overflow-y-auto rounded-lg border bg-background/60 p-3 font-mono text-2xs leading-relaxed">
        {rows.length === 0 ? (
          <div className="text-muted-foreground/60">No matching log lines.</div>
        ) : (
          rows.map((line, i) => (
            <div key={i} className="flex gap-2.5 py-px">
              <span className="shrink-0 text-muted-foreground/70">
                {line.time}
              </span>
              <span
                className={cn(
                  "w-12 shrink-0 font-semibold",
                  line.level === "INFO" && "text-primary",
                  line.level === "WARN" &&
                    "text-amber-600 dark:text-amber-400",
                  line.level === "ERROR" && "text-destructive"
                )}
              >
                {line.level}
              </span>
              <span className="shrink-0 text-muted-foreground/60">
                {line.replica}
              </span>
              <span className="text-foreground">{line.msg}</span>
            </div>
          ))
        )}
      </div>
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

interface EndpointRow {
  name: string
  status: string
  rps: string
  auth: string
  updated: string
}
const MOCK_ENDPOINTS: EndpointRow[] = [
  {
    name: "chat-completions",
    status: "healthy",
    rps: "1,204",
    auth: "api key",
    updated: "2d ago",
  },
  {
    name: "internal-eval",
    status: "healthy",
    rps: "36",
    auth: "none · internal",
    updated: "5d ago",
  },
]
const endpointColumns = [
  {
    key: "name",
    header: "Endpoint",
    cell: (e: EndpointRow) => (
      <Link to="/endpoints" className="inline-flex items-center gap-1 hover:underline">
        <span className="font-mono text-xs">{e.name}</span>
        <IconExternalLink className="size-3 text-muted-foreground" />
      </Link>
    ),
  },
  {
    key: "status",
    header: "Status",
    cell: (e: EndpointRow) => (
      <span className="inline-flex items-center gap-2">
        <StatusDot tone="ok" />
        <span className="text-2xs text-muted-foreground capitalize">
          {e.status}
        </span>
      </span>
    ),
  },
  { key: "rps", header: "Requests/sec", align: "right" as const, cell: (e: EndpointRow) => mono(e.rps) },
  {
    key: "auth",
    header: "Auth",
    cell: (e: EndpointRow) => (
      <span className="font-mono text-2xs text-muted-foreground">{e.auth}</span>
    ),
  },
  { key: "updated", header: "Updated", align: "right" as const, cell: (e: EndpointRow) => mono(e.updated) },
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
  { key: "percentile", header: "Percentile", cell: (r: LatencyRow) => mono(r.percentile) },
  { key: "ttft", header: "TTFT", align: "right" as const, cell: (r: LatencyRow) => mono(r.ttft) },
  { key: "itl", header: "Inter-token", align: "right" as const, cell: (r: LatencyRow) => mono(r.itl) },
  { key: "e2e", header: "End-to-end", align: "right" as const, cell: (r: LatencyRow) => mono(r.e2e) },
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
  { run: "1", seqs: "32", batch: "4096", kv: "16", spec: "Off", throughput: "12,100 tok/s", p99: "340ms", verdict: "Baseline" },
  { run: "2", seqs: "64", batch: "8192", kv: "16", spec: "Off", throughput: "16,900 tok/s", p99: "420ms", verdict: "Better" },
  { run: "3", seqs: "64", batch: "8192", kv: "32", spec: "Off", throughput: "18,420 tok/s", p99: "890ms", verdict: "Current" },
  { run: "4", seqs: "64", batch: "8192", kv: "32", spec: "On · draft 8b", throughput: "21,600 tok/s", p99: "610ms", verdict: "Needs soak" },
]
const sweepColumns = [
  { key: "run", header: "Run", cell: (r: SweepRow) => mono(r.run) },
  { key: "seqs", header: "max_num_seqs", align: "right" as const, cell: (r: SweepRow) => mono(r.seqs) },
  { key: "batch", header: "max_batch_tokens", align: "right" as const, cell: (r: SweepRow) => mono(r.batch) },
  { key: "kv", header: "KV block", align: "right" as const, cell: (r: SweepRow) => mono(r.kv) },
  { key: "spec", header: "Spec. decoding", cell: (r: SweepRow) => <span className="text-xs">{r.spec}</span> },
  { key: "throughput", header: "Throughput", align: "right" as const, cell: (r: SweepRow) => mono(r.throughput) },
  { key: "p99", header: "p99 latency", align: "right" as const, cell: (r: SweepRow) => mono(r.p99) },
  { key: "verdict", header: "Verdict", cell: (r: SweepRow) => <span className="text-xs">{r.verdict}</span> },
]

interface RunRow {
  config: string
  concurrency: string
  throughput: string
  ttft: string
  p99: string
  cost: string
  date: string
  current?: boolean
}
const MOCK_RUNS: RunRow[] = [
  { config: "fp8 · awq · 8x H100", concurrency: "64", throughput: "18,420 tok/s", ttft: "210ms", p99: "890ms", cost: "$4.20", date: "Aug 14", current: true },
  { config: "bf16 · 8x H100", concurrency: "64", throughput: "11,050 tok/s", ttft: "240ms", p99: "1,120ms", cost: "$7.85", date: "Aug 10" },
  { config: "fp8 · awq · 8x A100", concurrency: "64", throughput: "9,680 tok/s", ttft: "310ms", p99: "1,340ms", cost: "$3.10", date: "Aug 05" },
  { config: "fp8 · awq · 4x H100 (tp=4)", concurrency: "32", throughput: "12,300 tok/s", ttft: "180ms", p99: "760ms", cost: "$5.60", date: "Jul 30" },
]
const runColumns = [
  {
    key: "config",
    header: "Configuration",
    cell: (r: RunRow) => (
      <span className="text-xs">
        {r.config}
        {r.current && (
          <span className="ml-2 rounded-full bg-primary/15 px-1.5 py-0.5 font-mono text-2xs text-primary">
            current
          </span>
        )}
      </span>
    ),
  },
  { key: "concurrency", header: "Concurrency", align: "right" as const, cell: (r: RunRow) => mono(r.concurrency) },
  { key: "throughput", header: "Throughput", align: "right" as const, cell: (r: RunRow) => mono(r.throughput) },
  { key: "ttft", header: "p50 TTFT", align: "right" as const, cell: (r: RunRow) => mono(r.ttft) },
  { key: "p99", header: "p99 latency", align: "right" as const, cell: (r: RunRow) => mono(r.p99) },
  { key: "cost", header: "Cost / 1M tok", align: "right" as const, cell: (r: RunRow) => mono(r.cost) },
  { key: "date", header: "Date", align: "right" as const, cell: (r: RunRow) => mono(r.date) },
]

interface ReplicaRow {
  replica: string
  node: string
  cluster: string
  gpus: string
  interconnect: string
  type: string
  status: string
}
const MOCK_REPLICAS: ReplicaRow[] = [
  { replica: "replica-0", node: "node-0417", cluster: "us-east-1a", gpus: "0-1", interconnect: "NVLink", type: "On-demand", status: "healthy" },
  { replica: "replica-1", node: "node-0417", cluster: "us-east-1a", gpus: "2-3", interconnect: "NVLink", type: "On-demand", status: "healthy" },
  { replica: "replica-2", node: "node-0512", cluster: "us-east-1a", gpus: "0-1", interconnect: "NVLink", type: "On-demand", status: "healthy" },
  { replica: "replica-3", node: "node-0512", cluster: "us-east-1a", gpus: "2-3", interconnect: "NVLink", type: "On-demand", status: "healthy" },
  { replica: "replica-4", node: "node-0233", cluster: "us-east-1b", gpus: "0-1", interconnect: "NVLink", type: "On-demand", status: "healthy" },
  { replica: "replica-5", node: "node-0233", cluster: "us-east-1b", gpus: "2-3", interconnect: "NVLink", type: "On-demand", status: "healthy" },
]
const replicaColumns = [
  { key: "replica", header: "Replica", cell: (r: ReplicaRow) => mono(r.replica) },
  { key: "node", header: "Node", cell: (r: ReplicaRow) => mono(r.node) },
  { key: "cluster", header: "Cluster", cell: (r: ReplicaRow) => <span className="text-xs">{r.cluster}</span> },
  { key: "gpus", header: "GPUs", align: "right" as const, cell: (r: ReplicaRow) => mono(r.gpus) },
  { key: "interconnect", header: "Interconnect", cell: (r: ReplicaRow) => <span className="text-xs">{r.interconnect}</span> },
  { key: "type", header: "Type", cell: (r: ReplicaRow) => <span className="text-xs">{r.type}</span> },
  {
    key: "status",
    header: "Status",
    cell: (r: ReplicaRow) => (
      <span className="inline-flex items-center gap-2">
        <StatusDot tone="ok" />
        <span className="text-2xs text-muted-foreground capitalize">
          {r.status}
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
  { version: "v12", change: "llama-3.1-70b-chat-fp8 (recipe updated)", rollout: "Rolling", status: "Active", statusTone: "ok", deployed: "Aug 14" },
  { version: "v11", change: "Quantization bf16 → fp8", rollout: "Canary (10% → 100%)", status: "Superseded", statusTone: "neutral", deployed: "Aug 09" },
  { version: "v10", change: "Hardware 8x A100 → 8x H100", rollout: "Rolling", status: "Superseded", statusTone: "neutral", deployed: "Jul 30" },
  { version: "v9", change: "Initial release (bf16, 8x A100)", rollout: "Rolling", status: "Superseded", statusTone: "neutral", deployed: "Jul 28" },
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
  { key: "change", header: "Change", cell: (r: RevisionRow) => <span className="text-xs">{r.change}</span> },
  { key: "rollout", header: "Rollout", cell: (r: RevisionRow) => <span className="text-xs">{r.rollout}</span> },
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
  { key: "deployed", header: "Deployed", align: "right" as const, cell: (r: RevisionRow) => mono(r.deployed) },
]

interface LogLine {
  time: string
  level: "INFO" | "WARN" | "ERROR"
  replica: string
  msg: string
}
const MOCK_LOGS: LogLine[] = [
  { time: "08-24 14:01:58", level: "WARN", replica: "replica-1", msg: "queue depth exceeded threshold (128), triggering scale-up evaluation" },
  { time: "08-24 14:02:03", level: "INFO", replica: "autoscaler", msg: "scaling us-east-1a from 3 to 4 replicas" },
  { time: "08-24 14:02:41", level: "INFO", replica: "replica-3", msg: "model loaded, entering ready state (18.4s)" },
  { time: "08-24 14:02:44", level: "INFO", replica: "replica-3", msg: "passed readiness probe, added to load balancer" },
  { time: "08-24 14:13:57", level: "INFO", replica: "autoscaler", msg: "traffic normalized, queue depth nominal" },
  { time: "08-24 09:12:03", level: "ERROR", replica: "replica-4", msg: "CUDA out of memory during batch expansion, restarting worker" },
  { time: "08-24 09:12:05", level: "WARN", replica: "replica-4", msg: "restarting container (attempt 1/3)" },
  { time: "08-24 09:12:24", level: "INFO", replica: "replica-4", msg: "model loaded, entering ready state (19.1s)" },
  { time: "08-23 22:41:09", level: "INFO", replica: "replica-0", msg: "request batch flushed, size=32, avg_ttft=204ms" },
  { time: "08-18 02:14:00", level: "WARN", replica: "replica-5", msg: "node drain signal received, draining connections before eviction" },
  { time: "08-18 02:14:32", level: "INFO", replica: "scheduler", msg: "replica-5 rescheduled to node-0233, rejoined load balancer" },
]
