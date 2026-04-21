import { SurfaceCard } from "@/components/demand/endpoint-surface"
import { Field, FieldGrid } from "@/components/layout/field"
import { DeleteDialog } from "@/components/layout/destructive"
import { CopyableId, Dash, Id, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusDot, StatusLabel } from "@/components/layout/status-dot"
import { Sparkline } from "@/components/metrics/sparkline"
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
import type { MetricSeriesPoint } from "@/modules/metrics/types"
import type { Artifact, Deployment, Recipe } from "@/modules/demand/types"
import {
  useArtifactOptions,
  useDeleteEndpoint,
  useDeploymentOptions,
  useEndpoint,
  useRecipeOptions,
  useWorkloadOptions,
} from "@/modules/demand/queries"
import { EndpointForm } from "../EndpointForm"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { IconArrowLeft, IconArrowUpRight } from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"

/** A telemetry point trail, purely for the sparkline shape. */
function trail(values: number[]): MetricSeriesPoint[] {
  return values.map((value, at) => ({ at, value }))
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
  tone?: "default" | "ok" | "warn"
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
          tone === "warn" && "text-amber-500"
        )}
      >
        {value}
      </div>
      {children}
    </Card>
  )
}

/** The 14-day availability bar, one cell per day. */
function AvailabilityBar({ marks }: { marks: ("up" | "warn")[] }) {
  return (
    <div className="flex items-end gap-0.5">
      {marks.map((mark, i) => (
        <span
          key={i}
          className={cn(
            "h-2.5 w-[3px] rounded-[1px]",
            mark === "up" ? "bg-emerald-500" : "bg-amber-500"
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

/** `fp8 · awq` from a recipe's quantization and dtype, or null if neither is set. */
function quantLabel(recipe?: Recipe): string | null {
  const parts = [recipe?.quantization, recipe?.dtype].filter(
    (p): p is string => Boolean(p)
  )
  return parts.length > 0 ? parts.join(" · ") : null
}

/** What one endpoint target resolves to across deployment → workload → recipe → artifact. */
interface ResolvedTarget {
  deployment?: Deployment
  recipe?: Recipe
  artifact?: Artifact
}

export function EndpointDetailPage() {
  const { endpointId } = useParams({ from: "/_app/endpoints/$endpointId" })
  const { data: endpoint, isLoading, error } = useEndpoint(endpointId)
  const remove = useDeleteEndpoint()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)

  const { data: deploymentOptions } = useDeploymentOptions()
  const { data: workloadOptions } = useWorkloadOptions()
  const { data: recipeOptions } = useRecipeOptions()
  const { data: artifactOptions } = useArtifactOptions()

  /**
   * Resolves each endpoint target through the object graph the endpoint itself
   * does not carry: a target names only a deployment, and cluster, replicas,
   * recipe and artifact all hang off that deployment's workload and recipe. Every
   * lookup is defensive — a sparse payload or an un-drained option list leaves the
   * entry undefined and the cells fall back to a dash rather than throwing.
   */
  const resolved = useMemo(() => {
    const byDeployment = new Map<string, Deployment>(
      (deploymentOptions ?? []).map((d) => [d.id, d])
    )
    const workloadById = new Map(
      (workloadOptions ?? []).map((w) => [w.id, w])
    )
    const recipeById = new Map<string, Recipe>(
      (recipeOptions ?? []).map((r) => [r.id, r])
    )
    const artifactById = new Map<string, Artifact>(
      (artifactOptions ?? []).map((a) => [a.id, a])
    )

    const map = new Map<string, ResolvedTarget>()
    for (const target of endpoint?.targets ?? []) {
      const deployment = byDeployment.get(target.deployment_id)
      const workload = deployment?.workload_id
        ? workloadById.get(deployment.workload_id)
        : undefined
      const recipe = workload?.recipe_id
        ? recipeById.get(workload.recipe_id)
        : undefined
      const artifact = recipe?.artifact_id
        ? artifactById.get(recipe.artifact_id)
        : undefined
      map.set(target.deployment_id, { deployment, recipe, artifact })
    }
    return map
  }, [
    endpoint?.targets,
    deploymentOptions,
    workloadOptions,
    recipeOptions,
    artifactOptions,
  ])

  /** The recipe/artifact behind the primary target, for the right-hand card. */
  const backing = useMemo<ResolvedTarget | undefined>(() => {
    const targets = endpoint?.targets ?? []
    const primary = targets.find((t) => t.role === "primary") ?? targets[0]
    return primary ? resolved.get(primary.deployment_id) : undefined
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

  // TODO-API telemetry (requests/sec, latency percentiles, error rate,
  // availability) is data-plane and reaches the observability stack directly,
  // never the plane — mocked here so the summary and charts stay designed.
  const availability: ("up" | "warn")[] = [
    "up",
    "up",
    "up",
    "up",
    "up",
    "up",
    "up",
    "up",
    "warn",
    "up",
    "up",
    "up",
    "up",
    "up",
  ]

  // TODO-API the endpoint's public URL is a gateway fact, not on the object;
  // derived from the name here so the copy affordance is real.
  const url = endpoint
    ? `hangar.example.com/v1/${endpoint.name}`
    : ""

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
        actions={
          <div className="flex gap-2">
            {endpoint && (
              <>
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
              </>
            )}
            <Button asChild variant="outline" size="sm">
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
            {/* TODO-API summary tiles are telemetry (data-plane), mocked. */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
              <StatTile label="Requests / sec" value="1,204">
                <Sparkline
                  points={trail([14, 22, 26, 31, 40, 48, 52, 66])}
                  tone="primary"
                />
              </StatTile>
              <StatTile label="p50 latency" value="142ms" />
              <StatTile label="p99 latency" value="310ms" />
              <StatTile label="Error rate" value="0.02%" />
              <StatTile label="Availability (14d)" value="99.95%" tone="ok">
                <AvailabilityBar marks={availability} />
              </StatTile>
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              <div className="space-y-4 lg:col-span-2">
                {/* TODO-API traffic series is telemetry, mocked. */}
                <Card>
                  <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
                    <div>
                      <CardTitle className="text-sm">Traffic</CardTitle>
                      <p className="text-2xs text-muted-foreground">
                        Requests per second
                      </p>
                    </div>
                    <span className="font-mono text-2xs text-muted-foreground/60">
                      24h
                    </span>
                  </CardHeader>
                  <CardContent>
                    <Sparkline
                      points={trail([
                        20, 35, 40, 50, 70, 80, 75, 90, 85, 100, 95, 105, 102,
                        115, 118,
                      ])}
                      tone="primary"
                      size="lg"
                    />
                  </CardContent>
                </Card>

                {/* TODO-API latency percentiles are telemetry, mocked. */}
                <Card>
                  <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
                    <div>
                      <CardTitle className="text-sm">Latency</CardTitle>
                      <p className="text-2xs text-muted-foreground">
                        Response time percentiles
                      </p>
                    </div>
                    <span className="font-mono text-2xs text-muted-foreground/60">
                      24h
                    </span>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="flex flex-wrap items-center gap-4 text-2xs text-muted-foreground">
                      <span className="inline-flex items-center gap-1.5">
                        <span className="size-2 rounded-full bg-primary" />
                        p50 <span className="font-mono text-foreground">142ms</span>
                      </span>
                      <span className="inline-flex items-center gap-1.5">
                        <span className="size-2 rounded-full bg-amber-500" />
                        p95 <span className="font-mono text-foreground">238ms</span>
                      </span>
                      <span className="inline-flex items-center gap-1.5">
                        <span className="size-2 rounded-full bg-destructive" />
                        p99 <span className="font-mono text-foreground">310ms</span>
                      </span>
                    </div>
                    <div className="space-y-1">
                      <Sparkline
                        points={trail([30, 32, 30, 34, 33, 35, 34, 36, 35, 37, 36, 38, 37, 39, 38])}
                        tone="danger"
                      />
                      <Sparkline
                        points={trail([15, 18, 14, 22, 20, 26, 22, 30, 26, 34, 30, 40, 36, 45, 42])}
                        tone="warn"
                      />
                      <Sparkline
                        points={trail([2, 2, 0, 4, 3, 5, 4, 6, 5, 7, 6, 8, 7, 9, 8])}
                        tone="primary"
                      />
                    </div>
                  </CardContent>
                </Card>

                {/* Routing — real targets, weighted by target.weight. */}
                <Card>
                  <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
                    <div>
                      <CardTitle className="text-sm">Routing</CardTitle>
                      <p className="text-2xs text-muted-foreground">
                        How traffic is split across backing deployments
                      </p>
                    </div>
                    <Badge variant="outline" className="font-mono text-2xs">
                      {endpoint.routing_strategy}
                    </Badge>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <div className="space-y-2.5">
                      {(() => {
                        const total = endpoint.targets.reduce(
                          (sum, t) => sum + (t.enabled ? t.weight : 0),
                          0
                        )
                        return endpoint.targets.map((target) => {
                          const pct =
                            total > 0 && target.enabled
                              ? Math.round((target.weight / total) * 100)
                              : 0
                          return (
                            <div
                              key={target.deployment_id}
                              className="flex items-center gap-3"
                            >
                              <Link
                                to="/deployments/$deploymentId"
                                params={{ deploymentId: target.deployment_id }}
                                className="w-36 shrink-0 truncate font-mono text-2xs hover:underline"
                              >
                                {target.deployment_id}
                              </Link>
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
                              <div className="h-2 flex-1 overflow-hidden rounded-full bg-secondary">
                                <div
                                  className="h-full rounded-full bg-primary"
                                  style={{ width: `${pct}%` }}
                                />
                              </div>
                              <span className="w-11 shrink-0 text-right font-mono text-xs tabular-nums">
                                {pct}%
                              </span>
                            </div>
                          )
                        })
                      })()}
                      {endpoint.targets.length === 0 && (
                        <p className="py-6 text-center text-sm text-muted-foreground">
                          No targets — this endpoint routes nowhere.
                        </p>
                      )}
                    </div>

                    <div className="border-t pt-3">
                      <FieldGrid>
                        <Field label="Failover order">
                          <Dash>
                            {endpoint.failover_order.length > 0
                              ? endpoint.failover_order.join(" → ")
                              : null}
                          </Dash>
                        </Field>
                        {/* TODO-API region affinity is not on the endpoint object. */}
                        <Field label="Region affinity">
                          <span className="text-muted-foreground/60">
                            derived from targets
                          </span>
                        </Field>
                      </FieldGrid>
                    </div>
                  </CardContent>
                </Card>

                {/* Deployments — one row per target, resolved through the
                    deployment → workload → recipe → artifact graph. Cluster and
                    replicas are the deployment's observed state; recipe and
                    artifact are its workload's. All lookups are defensive. */}
                <Card className="overflow-hidden p-0">
                  <div className="border-b bg-muted/30 px-4 py-3">
                    <span className="font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
                      deployments
                    </span>
                    <span className="ml-2 font-mono text-2xs text-muted-foreground/40">
                      cluster and replicas are the deployment's; recipe and
                      artifact its workload's
                    </span>
                  </div>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Cluster</TableHead>
                        <TableHead>Role</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>Replicas</TableHead>
                        <TableHead>Recipe</TableHead>
                        <TableHead>Artifact</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {endpoint.targets.map((target) => {
                        const { deployment, recipe, artifact } =
                          resolved.get(target.deployment_id) ?? {}
                        const rollup = deployment?.replicas
                        const quant = quantLabel(recipe)
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
                                {deployment?.cluster_id ? (
                                  shortenId(deployment.cluster_id)
                                ) : (
                                  shortenId(target.deployment_id)
                                )}
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
                              {recipe ? (
                                <Link
                                  to="/recipes/$recipeId"
                                  params={{ recipeId: recipe.id }}
                                  className="inline-flex items-center gap-1 font-mono text-2xs hover:underline"
                                >
                                  {recipe.name}
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
                            colSpan={6}
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
                      <Field label="Endpoint URL" className="sm:col-span-2">
                        {/* TODO-API public URL is a gateway fact, derived. */}
                        <CopyableId value={`https://${url}`} className="text-2xs">
                          {url}
                        </CopyableId>
                      </Field>
                      <Field label="Name">{endpoint.name}</Field>
                      <Field label="Display name">
                        <Dash>{endpoint.display_name}</Dash>
                      </Field>
                      <Field label="Project">{endpoint.project_id}</Field>
                      <Field label="Visibility">{endpoint.visibility}</Field>
                      <Field label="Routing strategy">
                        {endpoint.routing_strategy}
                      </Field>
                      <Field label="Auth">
                        <Dash>{endpoint.surface?.auth_mode}</Dash>
                      </Field>
                      <Field label="Enabled">
                        {endpoint.enabled ? "yes" : "no"}
                      </Field>
                      <Field label="Created">
                        {new Date(endpoint.created_at).toLocaleDateString()}
                      </Field>
                      <Field label="Labels" className="sm:col-span-2">
                        <Labels labels={endpoint.labels} />
                      </Field>
                    </FieldGrid>
                  </CardContent>
                </Card>

                {/* Recipe & artifact — resolved through the primary target's
                    deployment. The endpoint carries none of this directly; it is
                    the backing workload's recipe and that recipe's artifact. */}
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Recipe &amp; artifact</CardTitle>
                  </CardHeader>
                  <CardContent>
                    {backing?.recipe || backing?.artifact ? (
                      <FieldGrid>
                        <Field label="Recipe">
                          {backing.recipe ? (
                            <Link
                              to="/recipes/$recipeId"
                              params={{ recipeId: backing.recipe.id }}
                              className="inline-flex items-center gap-1 font-mono text-2xs hover:underline"
                            >
                              {backing.recipe.name}
                              <IconArrowUpRight className="size-3 text-muted-foreground" />
                            </Link>
                          ) : (
                            <Dash>{null}</Dash>
                          )}
                        </Field>
                        <Field label="Engine">
                          <Dash>
                            {backing.recipe
                              ? [
                                  backing.recipe.engine,
                                  backing.recipe.engine_version,
                                ]
                                  .filter(Boolean)
                                  .join(" ")
                              : null}
                          </Dash>
                        </Field>
                        <Field label="Quantization">
                          <Dash>{quantLabel(backing.recipe)}</Dash>
                        </Field>
                        <Field label="Context length">
                          <Dash>
                            {backing.recipe?.context_length != null
                              ? backing.recipe.context_length.toLocaleString()
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
                        No backing recipe resolved for this endpoint's targets.
                      </p>
                    )}
                  </CardContent>
                </Card>

                <SurfaceCard surface={endpoint.surface} />

                {/* TODO-API endpoint activity/event history is not exposed on
                    the endpoint object; a designed timeline with mocked rows. */}
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Activity</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <ol className="space-y-3">
                      {[
                        {
                          tone: "warn" as const,
                          time: "Aug 24, 14:02",
                          text: "Latency degraded for 12m (p99 > 1.2s), recovered automatically",
                        },
                        {
                          tone: "info" as const,
                          time: "Aug 20, 09:15",
                          text: "Scaled a target from 3 → 4 replicas",
                        },
                        {
                          tone: "info" as const,
                          time: "Aug 14, 18:40",
                          text: "Recipe updated on backing deployment",
                        },
                        {
                          tone: "neutral" as const,
                          time: "Aug 02, 11:00",
                          text: "Endpoint created",
                        },
                      ].map((item, i) => (
                        <li key={i} className="flex gap-2.5">
                          <StatusDot tone={item.tone} className="mt-1" />
                          <div className="space-y-0.5">
                            <div className="font-mono text-2xs text-muted-foreground">
                              {item.time}
                            </div>
                            <div className="text-xs leading-relaxed">
                              {item.text}
                            </div>
                          </div>
                        </li>
                      ))}
                    </ol>
                  </CardContent>
                </Card>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
