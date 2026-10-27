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
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { DeploymentTimeline } from "@/components/demand/deployment-timeline"
import { EndpointPolicyCard } from "@/components/demand/endpoint-policy"
import { EndpointSurfaceCard } from "@/components/demand/endpoint-surface"
import { ConfirmDialog, DeleteDialog } from "@/components/layout/destructive"
import { Field, FieldGrid } from "@/components/layout/field"
import {
  CopyableId,
  Dash,
  Id,
  Labels,
  Truncated,
} from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
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
import { formatError, shortenId } from "@/lib/format"
import {
  useArtifactOptions,
  useDeleteEndpoint,
  useDeploymentOptions,
  useEndpoint,
  useUpdateEndpoint,
  useWorkloadOptions,
} from "@/modules/demand/queries"
import type { Artifact, Deployment } from "@/modules/demand/types"
import { useEndpointEvents } from "@/modules/machinery/queries"
import { sampleOf } from "@/modules/metrics/format"
import { useMetrics } from "@/modules/metrics/queries"
import { availabilityDays } from "@/modules/metrics/rollup"
import {
  DEFAULT_WINDOW,
  windowQuery,
  type TimeWindow,
} from "@/modules/metrics/windows"
import { EndpointForm } from "../EndpointForm"
import { AnalyticsView } from "./Analytics"
import { AVAILABILITY_DAYS, AVAILABILITY_SPAN } from "./Availability"
import {
  servingHealth,
  ReplicaBar,
  quantLabel,
  type ResolvedTarget,
} from "./Targets"

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
   * `fortnight` is the availability strip: a 14-day figure is a 14-day window,
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
   * other. Same metric, two spans, and the page shows both instead of picking:
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
   * lookup is defensive: a sparse payload or an un-drained option list leaves the
   * entry undefined and the cells fall back to a dash instead of throwing.
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

  /** The distinct regions this endpoint's targets land in: its region affinity. */
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
              immediately, and nothing behind it is torn down: resuming puts it
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
                          <CopyableId
                            value={endpoint.slug}
                            className="text-2xs"
                          >
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

                  {/* Routing: real targets, weighted, with the deployment's
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
                                  <span className="text-muted-foreground">
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
                              No targets: this endpoint routes nowhere.
                            </TableCell>
                          </TableRow>
                        )}
                      </TableBody>
                    </Table>
                  </Card>
                </div>

                <div className="space-y-4">
                  {/* Serving & artifact: the workload's serving config this
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
                        <p className="font-mono text-2xs text-muted-foreground">
                          No backing workload resolved for this endpoint's
                          targets.
                        </p>
                      )}
                    </CardContent>
                  </Card>

                  {/* Activity: the event log, filtered by this endpoint. The
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
              <AnalyticsView
                endpointId={endpointId}
                chartWindow={chartWindow}
                setChartWindow={setChartWindow}
                live={live}
                fortnight={fortnight}
                days={days}
                availableNow={availableNow}
                availableFortnight={availableFortnight}
                failure={failure}
              />
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
