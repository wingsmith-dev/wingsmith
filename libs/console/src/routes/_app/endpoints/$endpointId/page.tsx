import { DeploymentTimeline } from "@/components/demand/deployment-timeline"
import { Field, FieldGrid } from "@/components/layout/field"
import { DeleteDialog } from "@/components/layout/destructive"
import { CopyableId, Dash, Id, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
import { Sparkline } from "@/components/metrics/sparkline"
import { WindowToggle } from "@/components/metrics/window-toggle"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { formatError, shortenId } from "@/lib/format"
import { formatMetric, sampleOf } from "@/modules/metrics/format"
import { useMetrics } from "@/modules/metrics/queries"
import {
  availabilityDays,
  sumCurvePoints,
  type AvailabilityDay,
} from "@/modules/metrics/rollup"
import type { Metrics } from "@/modules/metrics/types"
import {
  DEFAULT_WINDOW,
  windowQuery,
  type TimeWindow,
} from "@/modules/metrics/windows"
import { useEndpointEvents } from "@/modules/machinery/queries"
import type { Artifact, Deployment, Workload } from "@/modules/demand/types"
import { footprintOfServing, footprintSummary } from "@/modules/demand/deploy"
import {
  useArtifactOptions,
  useDeleteEndpoint,
  useDeploymentOptions,
  useEndpoint,
  useWorkloadOptions,
} from "@/modules/demand/queries"
import { EndpointForm } from "../EndpointForm"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { IconArrowLeft, IconArrowUpRight } from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"

/** The strip's width, in days. */
const AVAILABILITY_DAYS = 14
const AVAILABILITY_SPAN = AVAILABILITY_DAYS * 86400

/** One metric as the console spells it, or an em dash where nothing measured it. */
function reading(data: Metrics | undefined, metric: string): string {
  if (!data) return "—"
  const sample = sampleOf(data.samples, metric)
  return sample ? formatMetric(sample.value, sample.unit) : "—"
}

/** Why a chart or a strip is empty, said where the chart would have been. */
function EmptyNote({ children }: React.PropsWithChildren) {
  return (
    <p className="py-2 font-mono text-2xs text-muted-foreground/50">
      {children}
    </p>
  )
}

/**
 * One curve, or a note saying the store held none.
 *
 * A `Sparkline` draws nothing at all below two points, so without this a metric
 * nobody is collecting and a metric that is flat at zero look identical.
 */
function Trend({
  data,
  metric,
  tone,
  size,
  label,
}: {
  data: Metrics
  metric: string
  tone: "primary" | "warn" | "danger"
  size?: "sm" | "lg"
  label: string
}) {
  const points = sumCurvePoints(data.series, metric)
  if (points.length < 2) {
    return <EmptyNote>no {label} series for this window</EmptyNote>
  }
  return <Sparkline points={points} tone={tone} size={size} />
}

/** One summary tile in the stat strip. */
function StatTile({
  label,
  value,
  tone = "default",
  children,
}: React.PropsWithChildren<{
  label: string
  value: string
  tone?: "default" | "ok" | "warn" | "danger"
}>) {
  return (
    <Card size="sm" className="gap-1.5 px-3 py-3">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </div>
      <div
        className={cn(
          "font-mono text-xl leading-none tabular-nums",
          tone === "ok" && "text-emerald-500",
          tone === "warn" && "text-amber-500",
          tone === "danger" && "text-destructive"
        )}
      >
        {value}
      </div>
      {children}
    </Card>
  )
}

/**
 * Where the console draws the line on an availability figure.
 *
 * The measurement carries no threshold of its own — whether 99.4% is an outage
 * is a claim somebody made, and the plane deliberately declines to make it. The
 * two numbers here are this console's reading colour and nothing else; they are
 * not an SLO and no acceptance term is checked against them.
 */
function availabilityTone(percent: number | null): "default" | "ok" | "warn" {
  if (percent === null) return "default"
  if (percent >= 99.9) return "ok"
  return "warn"
}

/**
 * The availability strip, one cell per day.
 *
 * A day the store answered nothing is drawn hollow rather than red. An endpoint
 * nobody called, and an endpoint the gateway's metrics port was never scraped
 * for, were not unavailable — and a strip that says they were is the one thing
 * this cell must never do.
 */
function AvailabilityBar({ days }: { days: AvailabilityDay[] }) {
  return (
    <div className="flex items-end gap-0.5">
      {days.map((day) => (
        <span
          key={day.at}
          title={`${new Date(day.at * 1000).toLocaleDateString()} · ${formatMetric(day.value, "percent")}`}
          className={cn(
            "h-2.5 w-[3px] rounded-[1px]",
            day.value === null
              ? "bg-muted-foreground/20"
              : day.value >= 99.9
                ? "bg-emerald-500"
                : day.value >= 99
                  ? "bg-amber-500"
                  : "bg-destructive"
          )}
        />
      ))}
    </div>
  )
}

/** A ready/total replica count with one cell per replica, ready cells lit. */
function ReplicaBar({ ready, total }: { ready: number; total: number }) {
  if (total <= 0) {
    return <span className="text-muted-foreground/40">—</span>
  }
  const cells = Math.min(total, 12)
  return (
    <span className="inline-flex items-center gap-2">
      <span className="font-mono text-xs tabular-nums">
        {ready}/{total}
      </span>
      <span className="flex gap-0.5">
        {Array.from({ length: cells }, (_, i) => (
          <span
            key={i}
            className={cn(
              "h-3 w-[5px] rounded-[1px]",
              i < ready ? "bg-emerald-500" : "bg-destructive"
            )}
          />
        ))}
      </span>
    </span>
  )
}

/** `fp8 · awq` from a serving block's quantization and dtype, or null if neither is set. */
function quantLabel(workload?: Workload): string | null {
  const parts = [
    workload?.serving.quantization,
    workload?.serving.dtype,
  ].filter((p): p is string => Boolean(p))
  return parts.length > 0 ? parts.join(" · ") : null
}

/** What one endpoint target resolves to across deployment → workload → artifact. */
interface ResolvedTarget {
  deployment?: Deployment
  workload?: Workload
  artifact?: Artifact
  /** The cluster region this target lands in, off the workload's deployment row. */
  region?: string | null
}

export function EndpointDetailPage() {
  const { endpointId } = useParams({ from: "/_app/endpoints/$endpointId" })
  const { data: endpoint, isLoading, error } = useEndpoint(endpointId)
  const remove = useDeleteEndpoint()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)
  const [chartWindow, setChartWindow] = useState<TimeWindow>(DEFAULT_WINDOW)

  const { data: deploymentOptions } = useDeploymentOptions()
  const { data: workloadOptions } = useWorkloadOptions()
  const { data: artifactOptions } = useArtifactOptions()

  /**
   * What the front door saw, which is the only place an endpoint is visible.
   *
   * An endpoint reaches no cluster, so no cluster can be asked about one and the
   * answer carries no cluster at all. Two queries because they answer two
   * different questions: `live` is whichever span the toggle names, and
   * `fortnight` is the availability strip — a 14-day figure is a 14-day window,
   * not fourteen of today's.
   */
  const live = useMetrics("endpoint", endpointId, windowQuery(chartWindow))
  const fortnight = useMetrics("endpoint", endpointId, {
    window: `${AVAILABILITY_DAYS}d`,
    series: true,
    span: AVAILABILITY_SPAN,
    step: "1h",
  })

  /** The endpoint's targets' events; nothing files one against the name itself. */
  const events = useEndpointEvents(endpoint?.id)

  const metrics = live.data
  const days = useMemo(
    () => availabilityDays(fortnight.data, AVAILABILITY_DAYS),
    [fortnight.data]
  )
  const availability = sampleOf(
    fortnight.data?.samples ?? [],
    "serving.availability"
  )
  const failure = sampleOf(metrics?.samples ?? [], "serving.failure_ratio")

  /**
   * Resolves each endpoint target through the object graph the endpoint itself
   * does not carry: a target names only a deployment, and cluster, replicas,
   * serving and artifact all hang off that deployment's workload. Every
   * lookup is defensive — a sparse payload or an un-drained option list leaves the
   * entry undefined and the cells fall back to a dash rather than throwing.
   */
  const resolved = useMemo(() => {
    const byDeployment = new Map<string, Deployment>(
      (deploymentOptions ?? []).map((d) => [d.id, d])
    )
    const workloadById = new Map((workloadOptions ?? []).map((w) => [w.id, w]))
    const artifactById = new Map<string, Artifact>(
      (artifactOptions ?? []).map((a) => [a.id, a])
    )

    const map = new Map<string, ResolvedTarget>()
    for (const target of endpoint?.targets ?? []) {
      const deployment = byDeployment.get(target.deployment_id)
      const workload = deployment?.workload_id
        ? workloadById.get(deployment.workload_id)
        : undefined
      const artifact = workload?.serving.artifact_id
        ? artifactById.get(workload.serving.artifact_id)
        : undefined
      const region = workload?.deployments.find(
        (d) => d.id === target.deployment_id
      )?.region
      map.set(target.deployment_id, { deployment, workload, artifact, region })
    }
    return map
  }, [endpoint?.targets, deploymentOptions, workloadOptions, artifactOptions])

  /** The serving block and artifact behind the primary target, for the right-hand card. */
  const backing = useMemo<ResolvedTarget | undefined>(() => {
    const targets = endpoint?.targets ?? []
    const primary = targets.find((t) => t.role === "primary") ?? targets[0]
    return primary ? resolved.get(primary.deployment_id) : undefined
  }, [endpoint?.targets, resolved])

  /** The distinct regions this endpoint's targets land in — its region affinity. */
  const regions = useMemo(() => {
    const seen = new Set<string>()
    for (const target of endpoint?.targets ?? []) {
      const region = resolved.get(target.deployment_id)?.region
      if (region) seen.add(region)
    }
    return [...seen]
  }, [endpoint?.targets, resolved])

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Endpoint" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load endpoint")}
        </div>
      </div>
    )
  }

  /**
   * Why the measurement half of this page is empty, when it is.
   *
   * Three different absences, and they must not be spelled the same way. No
   * store configured, a store nobody pointed at the gateway, and a store that
   * answered with no series are told apart by the plane and reported verbatim;
   * an endpoint nobody has called is not 100% available and is not 0% either.
   */
  const unmeasured = live.error
    ? formatError(live.error, "metrics unavailable")
    : (metrics?.unavailable ?? null)

  return (
    <div className="flex flex-col">
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            {endpoint?.name ?? "Endpoint"}
            {endpoint && (
              <StatusDot tone={endpoint.enabled ? "ok" : "neutral"} />
            )}
          </span>
        }
        description={endpoint?.id}
        readActions={
          <RefreshButton keys={[["endpoints"], ["metrics"], ["events"]]} />
        }
        actions={
          <div className="flex gap-2">
            {endpoint && (
              <>
                <Button variant="outline" onClick={() => setEditing(true)}>
                  Edit
                </Button>
                <Button variant="outline" onClick={() => setDoomed(true)}>
                  Delete
                </Button>
              </>
            )}
            <Button asChild variant="outline">
              <Link to="/endpoints">
                <IconArrowLeft className="size-4" />
                All endpoints
              </Link>
            </Button>
          </div>
        }
      />

      {endpoint && editing && (
        <EndpointForm
          key={endpoint.revision}
          open
          endpoint={endpoint}
          onOpenChange={(open) => !open && setEditing(false)}
        />
      )}
      {endpoint && (
        <DeleteDialog
          icon="endpoint"
          open={doomed}
          onOpenChange={setDoomed}
          kind="endpoint"
          name={endpoint.name}
          note="Clients calling this name start failing immediately."
          error={remove.error}
          pending={remove.isPending}
          onConfirm={() =>
            remove.mutate(endpoint.id, {
              onSuccess: () => {
                setDoomed(false)
                toast.success("Endpoint deleted", {
                  description: endpoint.name,
                })
                navigate({ to: "/endpoints" })
              },
              onError: (e) =>
                toast.error(formatError(e, "Could not delete endpoint")),
            })
          }
        />
      )}

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {endpoint && (
          <>
            {/* The gateway's own series, read at the front door. Absent
                measurements render as an em dash, never as a zero. The toggle
                governs the whole strip and both charts below it, because the
                tiles are the same query's instant values — a tile read over one
                lookback beside a curve drawn over another is two measurements
                wearing one heading. */}
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
                measured at the front door
              </span>
              <WindowToggle value={chartWindow} onChange={setChartWindow} />
            </div>
            {live.isLoading ? (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
                {[0, 1, 2, 3, 4].map((i) => (
                  <Skeleton key={i} className="h-20" />
                ))}
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
                <StatTile
                  label="Requests / sec"
                  value={reading(metrics, "serving.requests_per_second")}
                >
                  {metrics && (
                    <Trend
                      data={metrics}
                      metric="serving.requests_per_second"
                      tone="primary"
                      label="request rate"
                    />
                  )}
                </StatTile>
                <StatTile
                  label="p50 latency"
                  value={reading(metrics, "serving.latency_p50")}
                />
                <StatTile
                  label="p99 latency"
                  value={reading(metrics, "serving.latency_p99")}
                />
                <StatTile
                  label="Failed requests"
                  value={reading(metrics, "serving.failure_ratio")}
                  tone={
                    failure?.value == null
                      ? "default"
                      : failure.value > 1
                        ? "danger"
                        : failure.value > 0
                          ? "warn"
                          : "ok"
                  }
                />
                <StatTile
                  label={`Availability (${AVAILABILITY_DAYS}d)`}
                  value={
                    fortnight.isLoading
                      ? "…"
                      : formatMetric(availability?.value ?? null, "percent")
                  }
                  tone={availabilityTone(availability?.value ?? null)}
                >
                  <AvailabilityBar days={days} />
                </StatTile>
              </div>
            )}

            {unmeasured && (
              <Card size="sm" className="px-3 py-2.5">
                <p className="font-mono text-2xs text-muted-foreground/60">
                  {unmeasured}
                </p>
              </Card>
            )}

            <div className="grid gap-4 lg:grid-cols-3">
              <div className="space-y-4 lg:col-span-2">
                {/* Traffic — the gateway's request rate for this name. */}
                <Card>
                  <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
                    <div>
                      <CardTitle className="text-sm">Traffic</CardTitle>
                      <p className="text-2xs text-muted-foreground">
                        Requests per second, counted at the front door
                      </p>
                    </div>
                    <span className="font-mono text-2xs text-muted-foreground/60">
                      {chartWindow.label}
                    </span>
                  </CardHeader>
                  <CardContent>
                    {live.isLoading ? (
                      <Skeleton className="h-14" />
                    ) : metrics ? (
                      <Trend
                        data={metrics}
                        metric="serving.requests_per_second"
                        tone="primary"
                        size="lg"
                        label="request rate"
                      />
                    ) : (
                      <EmptyNote>nothing is measuring this endpoint</EmptyNote>
                    )}
                  </CardContent>
                </Card>

                {/* Latency — the gateway's percentiles, which include the hop
                    and every request that failed before an engine saw it. Not
                    the same measurement as a deployment's `inference.latency`,
                    and the two disagreeing is not a fault. */}
                <Card>
                  <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
                    <div>
                      <CardTitle className="text-sm">Latency</CardTitle>
                      <p className="text-2xs text-muted-foreground">
                        End-to-end percentiles, hop and queue included
                      </p>
                    </div>
                    <span className="font-mono text-2xs text-muted-foreground/60">
                      {chartWindow.label}
                    </span>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {live.isLoading ? (
                      <Skeleton className="h-24" />
                    ) : metrics ? (
                      <>
                        <div className="flex flex-wrap items-center gap-4 text-2xs text-muted-foreground">
                          <span className="inline-flex items-center gap-1.5">
                            <span className="size-2 rounded-full bg-destructive" />
                            p99{" "}
                            <span className="font-mono text-foreground">
                              {reading(metrics, "serving.latency_p99")}
                            </span>
                          </span>
                          <span className="inline-flex items-center gap-1.5">
                            <span className="size-2 rounded-full bg-amber-500" />
                            p95{" "}
                            <span className="font-mono text-foreground">
                              {reading(metrics, "serving.latency_p95")}
                            </span>
                          </span>
                          <span className="inline-flex items-center gap-1.5">
                            <span className="size-2 rounded-full bg-primary" />
                            p50{" "}
                            <span className="font-mono text-foreground">
                              {reading(metrics, "serving.latency_p50")}
                            </span>
                          </span>
                        </div>
                        <div className="space-y-1">
                          <Trend
                            data={metrics}
                            metric="serving.latency_p99"
                            tone="danger"
                            label="p99 latency"
                          />
                          <Trend
                            data={metrics}
                            metric="serving.latency_p95"
                            tone="warn"
                            label="p95 latency"
                          />
                          <Trend
                            data={metrics}
                            metric="serving.latency_p50"
                            tone="primary"
                            label="p50 latency"
                          />
                        </div>
                        <div className="border-t pt-3">
                          <FieldGrid>
                            <Field label="Time to first token (p50)">
                              {reading(metrics, "serving.ttft_p50")}
                            </Field>
                            <Field label="Time to first token (p95)">
                              {reading(metrics, "serving.ttft_p95")}
                            </Field>
                            <Field label="Requests in flight">
                              {reading(metrics, "serving.requests_in_flight")}
                            </Field>
                            <Field label="Tokens/sec in · out">
                              {reading(
                                metrics,
                                "serving.input_tokens_per_second"
                              )}{" "}
                              ·{" "}
                              {reading(
                                metrics,
                                "serving.output_tokens_per_second"
                              )}
                            </Field>
                          </FieldGrid>
                        </div>
                      </>
                    ) : (
                      <EmptyNote>nothing is measuring this endpoint</EmptyNote>
                    )}
                  </CardContent>
                </Card>

                {/* Routing — real targets, weighted, with the deployment's
                    observed state inlined. One card covers what traffic is sent
                    where and how it is doing. */}
                <Card className="gap-0 overflow-hidden p-0">
                  <div className="flex items-center justify-between gap-2 border-b bg-muted/30 px-4 py-3">
                    <span className="font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
                      routing
                    </span>
                    <Badge variant="outline" className="font-mono text-2xs">
                      {endpoint.routing_strategy}
                    </Badge>
                  </div>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Cluster</TableHead>
                        <TableHead>Role</TableHead>
                        <TableHead>Weight</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>Replicas</TableHead>
                        <TableHead>Serving</TableHead>
                        <TableHead>Artifact</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {endpoint.targets.map((target) => {
                        const { deployment, workload, artifact } =
                          resolved.get(target.deployment_id) ?? {}
                        const rollup = deployment?.replicas
                        const quant = quantLabel(workload)
                        const totalWeight = endpoint.targets.reduce(
                          (s, t) => s + (t.enabled ? t.weight : 0),
                          0
                        )
                        const weightPct =
                          totalWeight > 0 && target.enabled
                            ? Math.round((target.weight / totalWeight) * 100)
                            : 0
                        return (
                          <TableRow key={target.deployment_id}>
                            <TableCell>
                              <Link
                                to="/deployments/$deploymentId"
                                params={{
                                  deploymentId: target.deployment_id,
                                }}
                                className="inline-flex items-center gap-1 font-mono text-xs hover:underline"
                              >
                                {deployment?.cluster_id
                                  ? shortenId(deployment.cluster_id)
                                  : shortenId(target.deployment_id)}
                                <IconArrowUpRight className="size-3 text-muted-foreground" />
                              </Link>
                            </TableCell>
                            <TableCell>
                              <Badge
                                variant={
                                  target.role === "primary"
                                    ? "default"
                                    : "secondary"
                                }
                                className="font-mono text-2xs"
                              >
                                {target.role}
                              </Badge>
                            </TableCell>
                            <TableCell>
                              <div className="flex items-center gap-2">
                                <div className="h-1.5 w-16 overflow-hidden rounded-full bg-secondary">
                                  <div
                                    className="h-full rounded-full bg-primary"
                                    style={{ width: `${weightPct}%` }}
                                  />
                                </div>
                                <span className="font-mono text-2xs tabular-nums text-muted-foreground">
                                  {weightPct}%
                                </span>
                              </div>
                            </TableCell>
                            <TableCell>
                              <StatusLabel
                                tone={
                                  !target.enabled
                                    ? "neutral"
                                    : rollup != null &&
                                        rollup.ready >= rollup.total &&
                                        rollup.total > 0
                                      ? "ok"
                                      : "warn"
                                }
                              >
                                {!target.enabled
                                  ? "Disabled"
                                  : (deployment?.status?.phase ??
                                    deployment?.admission_phase ??
                                    "Pending")}
                              </StatusLabel>
                            </TableCell>
                            <TableCell>
                              {rollup != null ? (
                                <ReplicaBar
                                  ready={rollup.ready}
                                  total={rollup.total}
                                />
                              ) : (
                                <span className="text-muted-foreground/40">
                                  —
                                </span>
                              )}
                            </TableCell>
                            <TableCell>
                              {workload ? (
                                <Link
                                  to="/workloads/$workloadId"
                                  params={{ workloadId: workload.id }}
                                  className="inline-flex items-center gap-1 font-mono text-2xs hover:underline"
                                >
                                  {footprintSummary(
                                    footprintOfServing(workload.serving)
                                  )}
                                  <IconArrowUpRight className="size-3 text-muted-foreground" />
                                </Link>
                              ) : (
                                <span className="text-muted-foreground/40">
                                  —
                                </span>
                              )}
                            </TableCell>
                            <TableCell>
                              {artifact ? (
                                <div className="flex flex-col gap-0.5">
                                  <Link
                                    to="/artifacts/$artifactId"
                                    params={{ artifactId: artifact.id }}
                                    className="font-mono text-2xs hover:underline"
                                  >
                                    {artifact.name}
                                  </Link>
                                  {quant && (
                                    <span className="font-mono text-2xs text-muted-foreground">
                                      {quant}
                                    </span>
                                  )}
                                </div>
                              ) : (
                                <span className="text-muted-foreground/40">
                                  —
                                </span>
                              )}
                            </TableCell>
                          </TableRow>
                        )
                      })}
                      {endpoint.targets.length === 0 && (
                        <TableRow>
                          <TableCell
                            colSpan={7}
                            className="py-10 text-center text-sm text-muted-foreground"
                          >
                            No targets — this endpoint routes nowhere.
                          </TableCell>
                        </TableRow>
                      )}
                    </TableBody>
                  </Table>
                </Card>
              </div>

              <div className="space-y-4">
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Details</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <FieldGrid>
                      {/* The slug and not the name, which is what this panel
                          showed before and what made the object hard to read:
                          the gateway builds its route map on the slug, so a
                          page documenting the name was documenting a model id
                          that need not route. The gateway reads the `model` in
                          the request body and never branches on the path, so
                          this is the whole of the addressing, and the URL is
                          not ours — the gateway has no Ingress of its own and
                          is called by whatever sits in front of it. */}
                      <Field label="Slug" className="sm:col-span-2">
                        <CopyableId value={endpoint.slug} className="text-2xs">
                          {endpoint.slug}
                        </CopyableId>
                        <p className="mt-1 font-mono text-2xs text-muted-foreground/50">
                          Send this as <code>model</code> on the standard
                          OpenAI-compatible routes. It is the gateway's routing
                          key, so correcting it takes effect on the next poll
                          and restarts nothing. The engine behind it answers to
                          its artifact&rsquo;s slug, which is a different string
                          whenever this one is an alias; the gateway rewrites
                          between the two.
                        </p>
                      </Field>
                      <Field label="Name">
                        {endpoint.name}
                        <p className="mt-1 font-mono text-2xs text-muted-foreground/50">
                          The internal handle. Not what clients send.
                        </p>
                      </Field>
                      <Field label="Project">{endpoint.project_id}</Field>
                      <Field label="Visibility">{endpoint.visibility}</Field>
                      <Field label="Routing strategy">
                        {endpoint.routing_strategy}
                      </Field>
                      <Field label="Enabled">
                        {endpoint.enabled ? "yes" : "no"}
                      </Field>
                      <Field label="Created">
                        {new Date(endpoint.created_at).toLocaleDateString()}
                      </Field>
                      <Field label="Failover order">
                        <Dash>
                          {endpoint.failover_order.length > 0
                            ? endpoint.failover_order.join(" → ")
                            : null}
                        </Dash>
                      </Field>
                      <Field label="Region affinity">
                        <Dash>
                          {regions.length > 0 ? regions.join(", ") : null}
                        </Dash>
                      </Field>
                      <Field label="Labels" className="sm:col-span-2">
                        <Labels labels={endpoint.labels} />
                      </Field>
                    </FieldGrid>
                  </CardContent>
                </Card>

                {/* Serving & artifact — resolved through the primary target's
                    deployment. The endpoint carries none of this directly; it is
                    the backing workload's serving block and its artifact. */}
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">
                      Serving &amp; artifact
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    {backing?.workload || backing?.artifact ? (
                      <FieldGrid>
                        <Field label="Workload">
                          {backing.workload ? (
                            <Link
                              to="/workloads/$workloadId"
                              params={{ workloadId: backing.workload.id }}
                              className="inline-flex items-center gap-1 font-mono text-2xs hover:underline"
                            >
                              {backing.workload.name}
                              <IconArrowUpRight className="size-3 text-muted-foreground" />
                            </Link>
                          ) : (
                            <Dash>{null}</Dash>
                          )}
                        </Field>
                        <Field label="Engine">
                          <Dash>
                            {backing.workload
                              ? [
                                  backing.workload.serving.engine,
                                  backing.workload.serving.engine_version,
                                ]
                                  .filter(Boolean)
                                  .join(" ")
                              : null}
                          </Dash>
                        </Field>
                        <Field label="Quantization">
                          <Dash>{quantLabel(backing.workload)}</Dash>
                        </Field>
                        <Field label="Context length">
                          <Dash>
                            {backing.workload?.serving.context_length != null
                              ? backing.workload.serving.context_length.toLocaleString()
                              : null}
                          </Dash>
                        </Field>
                        <Field label="Artifact">
                          {backing.artifact ? (
                            <Link
                              to="/artifacts/$artifactId"
                              params={{ artifactId: backing.artifact.id }}
                              className="inline-flex items-center gap-1 font-mono text-2xs hover:underline"
                            >
                              {backing.artifact.name}
                              <IconArrowUpRight className="size-3 text-muted-foreground" />
                            </Link>
                          ) : (
                            <Dash>{null}</Dash>
                          )}
                        </Field>
                        <Field label="Fingerprint">
                          {backing.artifact?.digest ? (
                            <Id value={shortenId(backing.artifact.digest)} />
                          ) : (
                            <Dash>{null}</Dash>
                          )}
                        </Field>
                      </FieldGrid>
                    ) : (
                      <p className="font-mono text-2xs text-muted-foreground/60">
                        No backing workload resolved for this endpoint's
                        targets.
                      </p>
                    )}
                  </CardContent>
                </Card>

                {/* Activity — the event log, filtered by this endpoint. The
                    rows are its targets' deployments and their replicas: an
                    endpoint is a name and a weighting, and everything that can
                    go wrong with it goes wrong behind it. */}
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Activity</CardTitle>
                    <p className="text-2xs text-muted-foreground">
                      Recorded against the deployments this name routes to
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {events.error ? (
                      <p className="font-mono text-2xs text-destructive">
                        {formatError(events.error, "Could not read the log")}
                      </p>
                    ) : (
                      <DeploymentTimeline
                        events={events.items}
                        loading={events.isLoading}
                        empty={
                          endpoint.targets.length === 0
                            ? "This endpoint routes nowhere, so nothing can be recorded against it."
                            : "Nothing has been recorded against this endpoint's targets yet."
                        }
                      />
                    )}
                    {events.hasNextPage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-full"
                        disabled={events.isFetchingNextPage}
                        onClick={() => events.fetchNextPage()}
                      >
                        {events.isFetchingNextPage ? "Loading…" : "Load more"}
                      </Button>
                    )}
                  </CardContent>
                </Card>

                {/* Which store answered, and over what window. The gateway
                    belongs to no cluster, so this is never a cluster's store. */}
                {metrics?.source && (
                  <p className="font-mono text-2xs text-muted-foreground/50">
                    {metrics.window} window · {metrics.source.kind} ·{" "}
                    {metrics.source.url}
                  </p>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
