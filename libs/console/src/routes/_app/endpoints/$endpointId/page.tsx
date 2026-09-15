import { DeploymentTimeline } from "@/components/demand/deployment-timeline"
import { EndpointPolicyCard } from "@/components/demand/endpoint-policy"
import { EndpointSurfaceCard } from "@/components/demand/endpoint-surface"
import { Field, FieldGrid } from "@/components/layout/field"
import { ConfirmDialog, DeleteDialog } from "@/components/layout/destructive"
import {
  CopyableId,
  Dash,
  Id,
  Labels,
  Truncated,
} from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import {
  StatusDot,
  StatusLabel,
  type StatusTone,
} from "@/components/layout/status-dot"
import { MetricChart, type CurveTone } from "@/components/metrics/sparkline"
import { WindowToggle } from "@/components/metrics/window-toggle"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
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
import type {
  Artifact,
  Deployment,
  DeploymentReplicaRollup,
  Endpoint,
  Workload,
} from "@/modules/demand/types"
import {
  useArtifactOptions,
  useDeleteEndpoint,
  useDeploymentOptions,
  useEndpoint,
  useUpdateEndpoint,
  useWorkloadOptions,
} from "@/modules/demand/queries"
import { EndpointForm } from "../EndpointForm"
import { SlaCard } from "./SlaCard"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import {
  IconApi,
  IconArrowLeft,
  IconArrowUpRight,
  IconChartHistogram,
  IconListDetails,
  IconPencil,
  IconPlayerPause,
  IconPlayerPlay,
  IconShieldCheck,
  IconTrash,
} from "@tabler/icons-react"
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
 * A chart of one or more metrics, or a note saying the store held none.
 *
 * A chart draws nothing at all below two points, so without this a metric
 * nobody is collecting and a metric that is flat at zero look identical.
 *
 * The unit comes off the first metric's series, and every metric passed
 * together must share it — they are drawn against one axis, and a chart mixing
 * seconds with a rate would label that axis with a lie.
 */
function Chart({
  data,
  curves,
  spanLabel,
  fill,
  label,
}: {
  data: Metrics
  curves: { metric: string; tone: CurveTone }[]
  spanLabel: string
  fill?: boolean
  label: string
}) {
  const drawn = curves.map((curve) => ({
    tone: curve.tone,
    points: sumCurvePoints(data.series, curve.metric),
  }))
  if (drawn.every((curve) => curve.points.length < 2)) {
    return <EmptyNote>no {label} series for this window</EmptyNote>
  }
  /**
   * The unit off the first metric the store actually answered, not off the
   * first one asked for: a p99 nobody collected would otherwise label the axis
   * of the p50 drawn beside it.
   */
  const named = new Set(curves.map((curve) => curve.metric))
  const unit =
    data.series.find(
      (series) => named.has(series.metric) && series.points.length > 0
    )?.unit ?? "count"
  return (
    <MetricChart curves={drawn} unit={unit} spanLabel={spanLabel} fill={fill} />
  )
}

/** The reading colour for a figure, shared by every number on this page. */
type Tone = "default" | "ok" | "warn" | "danger"
const TONE_CLASS: Record<Tone, string> = {
  default: "",
  ok: "text-emerald-500",
  warn: "text-amber-500",
  danger: "text-destructive",
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
  tone?: Tone
}>) {
  return (
    <Card size="sm" className="gap-1.5 px-3 py-3">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </div>
      <div
        className={cn(
          "font-mono text-xl leading-none tabular-nums",
          TONE_CLASS[tone]
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
function availabilityTone(percent: number | null): Tone {
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
 *
 * The cells divide the width they are given, because `availabilityDays` always
 * returns the full fortnight: the strip is the same shape whatever the store
 * answered, and only the colours change.
 */
function AvailabilityBar({ days }: { days: AvailabilityDay[] }) {
  return (
    <div className="flex items-end gap-[3px]">
      {days.map((day) => (
        <span
          key={day.at}
          title={`${new Date(day.at * 1000).toLocaleDateString()} · ${formatMetric(day.value, "percent")}`}
          className={cn(
            "h-7 flex-1 rounded-[2px]",
            day.value === null
              ? "bg-muted-foreground/15"
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

/**
 * Availability twice: over the span the toggle names, and over the fortnight.
 *
 * The headline obeys the toggle, because a fortnight-wide figure is the wrong
 * answer to "how is this name doing" — a name that has been failing for an hour
 * still reads green across fourteen days, and that is precisely the moment
 * somebody is looking at this page.
 *
 * The strip below it stays fixed at fourteen days and says so. It is the other
 * question — not what this name is doing, but how it has behaved — which is why
 * it sits above the log rather than in the stat strip.
 *
 * Both are `100 - failure_ratio`, counted over requests and not over time: this
 * is the share of calls the front door answered, and an endpoint nobody called
 * has no figure at all rather than a perfect one. That is why an absent reading
 * is an em dash here and must never be drawn as 100%.
 */
function AvailabilityCard({
  windowLabel,
  loading,
  percent,
  historyLoading,
  history,
  days,
}: {
  windowLabel: string
  loading: boolean
  percent: number | null
  historyLoading: boolean
  history: number | null
  days: AvailabilityDay[]
}) {
  return (
    <Card size="sm" className="gap-2.5 px-4 py-3.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
          Availability
        </span>
        <span className="font-mono text-2xs text-muted-foreground/50">
          {windowLabel}
        </span>
      </div>
      <div
        className={cn(
          "font-mono text-xl leading-none tabular-nums",
          TONE_CLASS[availabilityTone(percent)]
        )}
      >
        {loading ? "…" : formatMetric(percent, "percent")}
      </div>
      <p className="font-mono text-2xs text-muted-foreground/50">
        the share of calls the front door answered
      </p>

      <div className="space-y-1.5 border-t pt-3">
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
            last {AVAILABILITY_DAYS} days
          </span>
          <span
            className={cn(
              "font-mono text-2xs tabular-nums",
              TONE_CLASS[availabilityTone(history)]
            )}
          >
            {historyLoading ? "…" : formatMetric(history, "percent")}
          </span>
        </div>
        <div className="w-full max-w-[420px] space-y-1.5">
          <AvailabilityBar days={days} />
          <div className="flex items-center justify-between font-mono text-2xs text-muted-foreground/40">
            <span>{AVAILABILITY_DAYS}d ago</span>
            <span>today</span>
          </div>
        </div>
      </div>
    </Card>
  )
}

/**
 * Whether this name can serve, which is not what `enabled` says.
 *
 * Read off the targets' replica rollups and never off the metrics store: an
 * endpoint nobody has called reports no series at all, so a measurement cannot
 * tell quiet from down, and a header dot that guesses is worse than one that
 * declines to answer. Until the deployment list has loaded there is no answer
 * and this says so rather than showing green.
 *
 * `enabled` is a separate reading and stays separate — declared and observed are
 * two different kinds of thing, and a name somebody deliberately turned off is
 * not a fault.
 */
function servingHealth(
  endpoint: Endpoint,
  rollups: (DeploymentReplicaRollup | undefined)[]
): { tone: StatusTone; label: string } {
  if (endpoint.targets.length === 0) {
    return { tone: "danger", label: "Routes nowhere" }
  }
  const known = rollups.filter((r): r is DeploymentReplicaRollup => r != null)
  if (known.length === 0) return { tone: "neutral", label: "Unknown" }
  const ready = known.reduce((sum, r) => sum + r.ready, 0)
  const total = known.reduce((sum, r) => sum + r.total, 0)
  if (ready === 0) return { tone: "danger", label: "Down" }
  if (ready < total) return { tone: "warn", label: "Degraded" }
  return { tone: "ok", label: "Serving" }
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
  const update = useUpdateEndpoint()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)
  const [pausing, setPausing] = useState(false)
  const [chartWindow, setChartWindow] = useState<TimeWindow>(DEFAULT_WINDOW)
  const [view, setView] = useState("overview")

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
  /**
   * Availability twice, because the toggle governs one of them and not the
   * other. Same metric, two spans, and the page shows both rather than picking:
   * a fortnight-wide figure hides an hour-old outage, and an hour-wide figure
   * hides a month of them.
   */
  const availableNow = sampleOf(metrics?.samples ?? [], "serving.availability")
  const availableFortnight = sampleOf(
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

  /** What the header dot reports. See `servingHealth`. */
  const health = useMemo(
    () =>
      endpoint
        ? servingHealth(
            endpoint,
            endpoint.targets
              .filter((target) => target.enabled)
              .map(
                (target) =>
                  resolved.get(target.deployment_id)?.deployment?.replicas
              )
          )
        : null,
    [endpoint, resolved]
  )

  const setEnabled = (enabled: boolean) => {
    if (!endpoint) return
    update.mutate(
      { ref: endpoint.id, body: { enabled }, revision: endpoint.revision },
      {
        onSuccess: () => {
          setPausing(false)
          toast.success(enabled ? "Endpoint resumed" : "Endpoint paused", {
            description: endpoint.name,
          })
        },
        onError: (e) =>
          toast.error(
            formatError(
              e,
              enabled ? "Could not resume endpoint" : "Could not pause endpoint"
            )
          ),
      }
    )
  }

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
        back={
          <>
            <Link
              to="/endpoints"
              aria-label="All endpoints"
              title="All endpoints"
              className="flex size-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <IconArrowLeft className="size-4" />
            </Link>
            <span className="h-8 w-px bg-border" />
          </>
        }
        title={
          <span className="flex items-center gap-2">
            {endpoint?.name ?? "Endpoint"}
            {health && (
              <span className="inline-flex items-center gap-1.5">
                <StatusDot
                  tone={health.tone}
                  pulse={health.tone === "danger"}
                />
                <span className="font-mono text-2xs font-normal tracking-wider text-muted-foreground uppercase">
                  {health.label}
                </span>
              </span>
            )}
            {endpoint && !endpoint.enabled && (
              <Badge variant="outline" className="font-mono text-2xs">
                paused
              </Badge>
            )}
          </span>
        }
        description={endpoint?.id}
        readActions={
          <RefreshButton keys={[["endpoints"], ["metrics"], ["events"]]} />
        }
        actions={
          endpoint && (
            <div className="flex gap-2">
              <Button
                variant="outline"
                disabled={update.isPending}
                onClick={() =>
                  endpoint.enabled ? setPausing(true) : setEnabled(true)
                }
              >
                {endpoint.enabled ? (
                  <>
                    <IconPlayerPause className="size-4" />
                    Pause
                  </>
                ) : (
                  <>
                    <IconPlayerPlay className="size-4" />
                    Resume
                  </>
                )}
              </Button>
              <Button variant="destructive" onClick={() => setDoomed(true)}>
                <IconTrash className="size-4" />
                Delete
              </Button>
            </div>
          )
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
        <ConfirmDialog
          icon="endpoint"
          open={pausing}
          onOpenChange={setPausing}
          title="Pause endpoint?"
          description={
            <>
              <span className="font-mono text-foreground">{endpoint.name}</span>{" "}
              stops answering. Clients calling this name start failing
              immediately, and nothing behind it is torn down — resuming puts it
              back.
            </>
          }
          confirmLabel="Pause"
          pendingLabel="Pausing…"
          error={update.error}
          pending={update.isPending}
          onConfirm={() => setEnabled(false)}
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

      {isLoading && (
        <div className="p-4">
          <Skeleton className="h-40 w-full" />
        </div>
      )}

      {endpoint && (
        <Tabs value={view} onValueChange={setView} className="flex-col gap-0">
          <TabsList variant="underline" className="bg-card px-4">
            <ViewTab value="overview" icon={IconListDetails}>
              Overview
            </ViewTab>
            <ViewTab value="api-surface" icon={IconApi}>
              API surface
            </ViewTab>
            <ViewTab value="policies" icon={IconShieldCheck}>
              Policies
            </ViewTab>
            <ViewTab value="analytics" icon={IconChartHistogram}>
              Analytics
            </ViewTab>
          </TabsList>

          <div className="space-y-4 p-4">
            {/* ---- Overview ---- */}
            <TabsContent value="overview">
              <div className="grid gap-4 lg:grid-cols-3">
                <div className="space-y-4 lg:col-span-2">
                  {/* Declared fields, and the workload's serving config this
                      name ultimately runs. */}
                  <Card>
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm">Details</CardTitle>
                      <CardAction>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label="Edit endpoint"
                          onClick={() => setEditing(true)}
                        >
                          <IconPencil />
                        </Button>
                      </CardAction>
                    </CardHeader>
                    <CardContent className="space-y-4">
                      <FieldGrid>
                        <Field label="Slug">
                          <CopyableId value={endpoint.slug} className="text-2xs">
                            {endpoint.slug}
                          </CopyableId>
                        </Field>
                        <Field label="Aliases">
                          <Dash>
                            {endpoint.aliases.length > 0
                              ? endpoint.aliases.join(", ")
                              : null}
                          </Dash>
                        </Field>
                        <Field label="Name">{endpoint.name}</Field>
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
                        <Field label="Labels">
                          <Labels labels={endpoint.labels} />
                        </Field>
                      </FieldGrid>
                    </CardContent>
                  </Card>

                  {/* Routing — real targets, weighted, with the deployment's
                      observed state inlined. One card covers what traffic is
                      sent where and how it is doing. */}
                  <Card className="gap-0 overflow-hidden p-0">
                    <div className="border-b bg-muted/30 px-4 py-3">
                      <div className="flex items-center justify-between">
                        <CardTitle className="text-sm">Routing</CardTitle>
                        <div className="flex items-center gap-2">
                          <Badge
                            variant="outline"
                            className="font-mono text-2xs"
                          >
                            {endpoint.routing_strategy}
                          </Badge>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label="Edit routing"
                            onClick={() => setEditing(true)}
                          >
                            <IconPencil />
                          </Button>
                        </div>
                      </div>
                      <p className="mt-1 text-2xs text-muted-foreground">
                        How traffic is split across backing deployments
                      </p>
                    </div>
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Deployment</TableHead>
                          <TableHead>Role</TableHead>
                          <TableHead>Weight</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Replicas</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {endpoint.targets.map((target) => {
                          const { deployment, region } =
                            resolved.get(target.deployment_id) ?? {}
                          const rollup = deployment?.replicas
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
                              <TableCell className="py-3.5">
                                <Link
                                  to="/deployments/$deploymentId"
                                  params={{
                                    deploymentId: target.deployment_id,
                                  }}
                                  className="group inline-flex flex-col gap-0.5"
                                >
                                  <span className="inline-flex items-center gap-1 font-mono text-xs group-hover:underline">
                                    {deployment?.name ??
                                      shortenId(target.deployment_id)}
                                    <IconArrowUpRight className="size-3 text-muted-foreground" />
                                  </span>
                                  {region && (
                                    <span className="font-mono text-2xs text-muted-foreground">
                                      {region}
                                    </span>
                                  )}
                                </Link>
                              </TableCell>
                              <TableCell className="py-3.5">
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
                              <TableCell className="py-3.5">
                                <div className="flex items-center gap-2">
                                  <div className="h-1.5 w-16 overflow-hidden rounded-full bg-secondary">
                                    <div
                                      className="h-full rounded-full bg-primary"
                                      style={{ width: `${weightPct}%` }}
                                    />
                                  </div>
                                  <span className="font-mono text-2xs text-muted-foreground tabular-nums">
                                    {weightPct}%
                                  </span>
                                </div>
                              </TableCell>
                              <TableCell className="py-3.5">
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
                              <TableCell className="py-3.5">
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
                            </TableRow>
                          )
                        })}
                        {endpoint.targets.length === 0 && (
                          <TableRow>
                            <TableCell
                              colSpan={5}
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
                  {/* Serving & artifact — the workload's serving config this
                      name ultimately runs. */}
                  <Card>
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm">
                        Serving &amp; artifact
                      </CardTitle>
                    </CardHeader>
                    <CardContent>
                      {backing?.workload || backing?.artifact ? (
                        <FieldGrid className="sm:grid-cols-1">
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
                                    backing.workload.serving
                                      .engine_runtime_version,
                                  ]
                                    .filter(Boolean)
                                    .join(" ")
                                : null}
                            </Dash>
                          </Field>
                          <Field label="Frontend image">
                            {backing.workload?.serving.frontend_image ? (
                              <Truncated
                                value={backing.workload.serving.frontend_image}
                              />
                            ) : (
                              <Dash>{null}</Dash>
                            )}
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
                      endpoint is a name and a weighting, and everything that
                      can go wrong with it goes wrong behind it. */}
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
                </div>
              </div>
            </TabsContent>

            {/* ---- API surface ---- */}
            <TabsContent value="api-surface">
              <EndpointSurfaceCard surface={endpoint.surface} />
            </TabsContent>

            {/* ---- Policies ---- */}
            <TabsContent value="policies">
              <EndpointPolicyCard policy={endpoint.surface?.policy} />
            </TabsContent>

            {/* ---- Analytics ---- */}
            <TabsContent value="analytics" className="space-y-4">
              {/* The gateway's own series, read at the front door. Absent
                  measurements render as an em dash, never as a zero. The toggle
                  governs the stat strip and both charts under it, because the
                  tiles are the same query's instant values — a tile read over
                  one lookback beside a curve drawn over another is two
                  measurements wearing one heading. Availability is not in that
                  strip and names its own span, for that same reason. */}
              <div className="flex items-center justify-end gap-2">
                <WindowToggle value={chartWindow} onChange={setChartWindow} />
              </div>

              <div className="grid gap-4 lg:grid-cols-3">
                <div className="space-y-4 lg:col-span-2">
                  {/* The strip carries only what no card below it repeats: a
                      rate, the percentile people scan for, and the failure
                      share. p50 and p95 are in the latency card's legend, and
                      putting them here as well is the same number twice. */}
                  {live.isLoading ? (
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                      {[0, 1, 2].map((i) => (
                        <Skeleton key={i} className="h-[68px]" />
                      ))}
                    </div>
                  ) : (
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                      <StatTile
                        label="Requests / sec"
                        value={reading(metrics, "serving.requests_per_second")}
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
                    </div>
                  )}

                  {unmeasured && (
                    <Card size="sm" className="px-3 py-2.5">
                      <p className="font-mono text-2xs text-muted-foreground/60">
                        {unmeasured}
                      </p>
                    </Card>
                  )}

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
                        <Skeleton className="h-28" />
                      ) : metrics ? (
                        <Chart
                          data={metrics}
                          curves={[
                            {
                              metric: "serving.requests_per_second",
                              tone: "primary",
                            },
                          ]}
                          spanLabel={chartWindow.label}
                          fill
                          label="request rate"
                        />
                      ) : (
                        <EmptyNote>nothing is measuring this endpoint</EmptyNote>
                      )}
                    </CardContent>
                  </Card>

                  {/* Latency — the gateway's percentiles, which include the hop
                      and every request that failed before an engine saw it. Not
                      the same measurement as a deployment's
                      `inference.latency`, and the two disagreeing is not a
                      fault. */}
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
                          <Chart
                            data={metrics}
                            curves={[
                              { metric: "serving.latency_p99", tone: "danger" },
                              { metric: "serving.latency_p95", tone: "warn" },
                              { metric: "serving.latency_p50", tone: "primary" },
                            ]}
                            spanLabel={chartWindow.label}
                            label="latency"
                          />
                          <div className="flex flex-wrap items-center gap-4 pl-[72px] text-2xs text-muted-foreground">
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
                </div>

                <div className="space-y-4">
                  <AvailabilityCard
                    windowLabel={chartWindow.label}
                    loading={live.isLoading}
                    percent={availableNow?.value ?? null}
                    historyLoading={fortnight.isLoading}
                    history={availableFortnight?.value ?? null}
                    days={days}
                  />
                  <SlaCard endpointId={endpointId} />
                </div>
              </div>
            </TabsContent>

          </div>
        </Tabs>
      )}
    </div>
  )
}

/** One tab in the underline list, with its icon. */
function ViewTab({
  value,
  icon: Icon,
  children,
}: React.PropsWithChildren<{
  value: string
  icon: typeof IconApi
}>) {
  return (
    <TabsTrigger value={value}>
      <Icon />
      {children}
    </TabsTrigger>
  )
}
