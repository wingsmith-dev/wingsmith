import { DataTable, type Column } from "@/components/layout/data-table"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusDot, type StatusTone } from "@/components/layout/status-dot"
import { Sparkline } from "@/components/metrics/sparkline"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { formatError, shortenId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useProjectOptions } from "@/modules/binding/queries"
import {
  useArtifactOptions,
  useDeleteEndpoint,
  useDeploymentOptions,
  useEndpoints,
  useWorkloadOptions,
} from "@/modules/demand/queries"
import type {
  Artifact,
  Deployment,
  Endpoint,
  Workload,
} from "@/modules/demand/types"
import { formatMetric, valueOf } from "@/modules/metrics/format"
import { useServingMetrics } from "@/modules/metrics/queries"
import type { MetricSample, MetricSeriesPoint } from "@/modules/metrics/types"
import { IconArrowUpRight } from "@tabler/icons-react"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { EndpointForm } from "./EndpointForm"

/**
 * A stable pseudo-random seed from an id, so a row's sparkline stays put across
 * renders rather than flickering each paint.
 */
function seed(id: string): number {
  let hash = 0
  for (let i = 0; i < id.length; i++) {
    hash = (hash * 31 + id.charCodeAt(i)) | 0
  }
  return Math.abs(hash)
}

/**
 * A short series shaped by the seed, for sparkline form only.
 *
 * TODO-API the numbers beside these curves are the gateway's own, but the shape
 * is not: `/metrics/serving` breaks down per endpoint in samples and draws its
 * curves from the aggregate expression, so there is no per-endpoint series to
 * plot without a query per row. Give the serving scope per-endpoint curves and
 * this goes away.
 */
function mockTrail(id: string, salt: number): MetricSeriesPoint[] {
  const base = seed(`${id}:${salt}`)
  return Array.from({ length: 8 }, (_, at) => ({
    at,
    value: 10 + ((base >> at) % 7) + at * ((base % 3) - 1),
  }))
}

interface Health {
  tone: StatusTone
  label: string
  availability: string
}

/**
 * What an endpoint's row says about it, read off the gateway's own series.
 *
 * Only `enabled` is a plane fact; everything else is measurement, and where the
 * store answered nothing the row says so rather than guessing. An endpoint
 * nobody has called is not down and is not 100% up — it is unmeasured, and the
 * two must not be spelled the same way.
 *
 * The thresholds are this console's reading colour and nothing else. No
 * acceptance term is checked against them.
 */
function health(
  endpoint: Endpoint,
  availability: number | null,
  measured: boolean
): Health {
  if (!endpoint.enabled) {
    return { tone: "neutral", label: "Disabled", availability: "—" }
  }
  if (!measured || availability === null) {
    return { tone: "neutral", label: "Unmeasured", availability: "—" }
  }
  const tone: StatusTone =
    availability >= 99.9 ? "ok" : availability >= 99 ? "warn" : "danger"
  return {
    tone,
    label: tone === "ok" ? "Healthy" : tone === "warn" ? "Degraded" : "Down",
    availability: formatMetric(availability, "percent"),
  }
}

/** A weighted target's readiness bar — enabled targets up, the rest idle. */
function TargetBar({ enabled, total }: { enabled: number; total: number }) {
  return (
    <div className="flex gap-0.5">
      {Array.from({ length: total }, (_, i) => (
        <span
          key={i}
          className={cn(
            "h-3 w-[5px] rounded-[1px]",
            i < enabled ? "bg-emerald-500" : "bg-border"
          )}
        />
      ))}
    </div>
  )
}

/** One summary tile in the stat strip. */
function StatTile({
  label,
  value,
  tone = "default",
}: {
  label: string
  value: string
  tone?: "default" | "ok"
}) {
  return (
    <Card size="sm" className="gap-1.5 px-4 py-3">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </div>
      <div
        className={cn(
          "font-mono text-xl leading-none tabular-nums",
          tone === "ok" && "text-emerald-500"
        )}
      >
        {value}
      </div>
    </Card>
  )
}

/**
 * What an endpoint's primary target resolves to across the object graph the
 * endpoint does not carry — deployment → workload → artifact — plus the regions its
 * targets land in, read off the workload's per-cluster deployments.
 */
interface ResolvedEndpoint {
  workload?: Workload
  artifact?: Artifact
  regions: string[]
}

/**
 * Builds a resolver over drained option lists. A sparse payload or an un-drained
 * list leaves a field undefined and the cell falls back to a dash rather than
 * throwing.
 */
function makeResolver(
  deployments: Deployment[],
  workloads: Workload[],
  artifacts: Artifact[]
): (endpoint: Endpoint) => ResolvedEndpoint {
  const deploymentById = new Map(deployments.map((d) => [d.id, d]))
  const workloadById = new Map(workloads.map((w) => [w.id, w]))
  const artifactById = new Map(artifacts.map((a) => [a.id, a]))

  return (endpoint: Endpoint) => {
    const targets = endpoint.targets ?? []
    const primary = targets.find((t) => t.role === "primary") ?? targets[0]
    const deployment = primary
      ? deploymentById.get(primary.deployment_id)
      : undefined
    const workload = deployment?.workload_id
      ? workloadById.get(deployment.workload_id)
      : undefined
    const artifact = workload?.serving.artifact_id
      ? artifactById.get(workload.serving.artifact_id)
      : undefined

    const regions = new Set<string>()
    for (const target of targets) {
      const dep = deploymentById.get(target.deployment_id)
      const wl = dep?.workload_id
        ? workloadById.get(dep.workload_id)
        : undefined
      const region = wl?.deployments.find(
        (d) => d.id === target.deployment_id
      )?.region
      if (region) regions.add(region)
    }

    return { workload, artifact, regions: [...regions] }
  }
}

/**
 * One endpoint's measurement, out of the gateway's breakdown.
 *
 * The two windows are two different questions, so they are two queries and two
 * lookups: `now` is the rate and the percentiles at the plane's own window, and
 * `availability` is a fortnight. Neither is the other resampled.
 */
interface Reading {
  now: MetricSample[]
  availability: number | null
  measured: boolean
}

function columns(
  onEdit: (endpoint: Endpoint) => void,
  onDelete: (endpoint: Endpoint) => void,
  resolve: (endpoint: Endpoint) => ResolvedEndpoint,
  read: (endpoint: Endpoint) => Reading
): Column<Endpoint>[] {
  return [
    {
      key: "name",
      header: "Endpoint",
      cell: (endpoint) => (
        <ResourceCell kind="endpoint">
          <Link
            to="/endpoints/$endpointId"
            params={{ endpointId: endpoint.id }}
            className="block"
          >
            <span
              className="block max-w-[16rem] truncate font-mono text-xs"
              title={endpoint.name}
            >
              {endpoint.name}
            </span>
            <Id value={shortenId(endpoint.id)} />
          </Link>
        </ResourceCell>
      ),
    },
    {
      key: "status",
      header: (
        <span>
          Status{" "}
          <span className="text-muted-foreground/50 normal-case">/ 14d</span>
        </span>
      ),
      cell: (endpoint) => {
        const { availability, measured } = read(endpoint)
        const state = health(endpoint, availability, measured)
        return (
          <div className="flex flex-col gap-1.5">
            <span className="inline-flex items-center gap-2">
              <StatusDot tone={state.tone} />
              <span className="text-2xs text-muted-foreground">
                {state.label}
              </span>
            </span>
            <span className="font-mono text-2xs text-muted-foreground/60">
              {state.availability}
            </span>
          </div>
        )
      },
    },
    {
      key: "workload",
      header: "Workload",
      secondary: true,
      cell: (endpoint) => {
        const { workload } = resolve(endpoint)
        if (!workload) return <Dash>{null}</Dash>
        return (
          <Link
            to="/workloads/$workloadId"
            params={{ workloadId: workload.id }}
            className="inline-flex max-w-[12rem] items-center gap-1 font-mono text-2xs text-muted-foreground hover:underline"
            title={workload.name}
          >
            <span className="truncate">{workload.name}</span>
            <IconArrowUpRight className="size-3 text-muted-foreground/50" />
          </Link>
        )
      },
    },
    {
      key: "traffic",
      header: "Traffic",
      cell: (endpoint) => {
        const { now, availability, measured } = read(endpoint)
        const rate = valueOf(now, "serving.requests_per_second")
        if (rate === null) return <Dash>{null}</Dash>
        const tone = health(endpoint, availability, measured).tone
        return (
          <div className="flex flex-col gap-1">
            {/* TODO-API the curve is mocked; the figure under it is not. */}
            <div className="w-14">
              <Sparkline
                points={mockTrail(endpoint.id, 1)}
                tone={tone === "ok" ? "primary" : "warn"}
              />
            </div>
            <span className="font-mono text-2xs tabular-nums">
              {formatMetric(rate, "per_second")}
            </span>
          </div>
        )
      },
    },
    {
      key: "latency",
      header: "Latency (p50 / p99)",
      secondary: true,
      cell: (endpoint) => {
        const { now, availability, measured } = read(endpoint)
        const p50 = valueOf(now, "serving.latency_p50")
        const p99 = valueOf(now, "serving.latency_p99")
        if (p50 === null && p99 === null) return <Dash>{null}</Dash>
        const tone = health(endpoint, availability, measured).tone
        return (
          <div className="flex flex-col gap-1">
            {/* TODO-API the curve is mocked; the figures under it are not. */}
            <div className="w-14">
              <Sparkline
                points={mockTrail(endpoint.id, 2)}
                tone={tone === "ok" ? "primary" : "warn"}
              />
            </div>
            <span className="font-mono text-2xs tabular-nums">
              {formatMetric(p50, "seconds")} / {formatMetric(p99, "seconds")}
            </span>
          </div>
        )
      },
    },
    {
      key: "targets",
      header: "Targets",
      cell: (endpoint) => {
        const targets = endpoint.targets ?? []
        const enabled = targets.filter((t) => t.enabled).length
        return (
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs tabular-nums">
              {enabled}/{targets.length}
            </span>
            <TargetBar enabled={enabled} total={targets.length} />
          </div>
        )
      },
    },
    {
      key: "region",
      header: "Region",
      secondary: true,
      cell: (endpoint) => {
        const { regions } = resolve(endpoint)
        if (regions.length === 0) return <Dash>{null}</Dash>
        return (
          <span
            className="block max-w-[10rem] truncate font-mono text-2xs text-muted-foreground"
            title={regions.join(", ")}
          >
            {regions.join(", ")}
          </span>
        )
      },
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (endpoint) => (
        <RowActions
          actions={[
            { label: "Edit", onSelect: () => onEdit(endpoint) },
            {
              label: "Delete",
              destructive: true,
              onSelect: () => onDelete(endpoint),
            },
          ]}
        />
      ),
    },
  ]
}

/**
 * An endpoint has no cluster at any depth. It spans clusters by construction —
 * that is what the weighted target set is for — so there is nothing here to
 * scope by cluster and no column pretending otherwise.
 */
export function EndpointsPage() {
  const search = useSearch({ from: "/_app/endpoints" })
  const navigate = useNavigate({ from: "/endpoints" })
  const { data: projects } = useProjectOptions()
  const list = useEndpoints({ project: search.project })
  const { data: deploymentOptions } = useDeploymentOptions()
  const { data: workloadOptions } = useWorkloadOptions()
  const { data: artifactOptions } = useArtifactOptions()
  const remove = useDeleteEndpoint()

  /**
   * The gateway's own answer for the whole front door, in two windows.
   *
   * One query for the page rather than one per row: the totals are the gateway's
   * and do not move when somebody pages, and a table of names must not be a
   * round trip to Prometheus per name. `fortnight` exists only for the
   * availability column, because a 14-day figure is a 14-day window and not
   * fourteen of today's.
   */
  const serving = useServingMetrics()
  const fortnight = useServingMetrics({ window: "14d" })

  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Endpoint | undefined>()
  const [doomed, setDoomed] = useState<Endpoint | undefined>()

  const resolve = useMemo(
    () =>
      makeResolver(
        deploymentOptions ?? [],
        workloadOptions ?? [],
        artifactOptions ?? []
      ),
    [deploymentOptions, workloadOptions, artifactOptions]
  )

  /**
   * Each endpoint's own row out of the two breakdowns, keyed the way the
   * gateway labels them — by endpoint id. A name the store has never seen is
   * absent from the breakdown and reads as unmeasured, which is what it is.
   */
  const read = useMemo(() => {
    const now = new Map(
      (serving.data?.breakdown ?? []).map((row) => [row.key, row.samples])
    )
    const days = new Map(
      (fortnight.data?.breakdown ?? []).map((row) => [row.key, row.samples])
    )
    return (endpoint: Endpoint) => {
      const samples = now.get(endpoint.id)
      return {
        now: samples ?? [],
        availability: valueOf(
          days.get(endpoint.id) ?? [],
          "serving.availability"
        ),
        measured: Boolean(samples),
      }
    }
  }, [serving.data, fortnight.data])

  const endpoints = list.items ?? []
  const total = endpoints.length
  const enabled = endpoints.filter((e) => e.enabled).length
  const requestsPerSec = valueOf(
    serving.data?.samples ?? [],
    "serving.requests_per_second"
  )
  const p50 = valueOf(serving.data?.samples ?? [], "serving.latency_p50")

  /** Why the measured columns are empty, when they are. Never drawn as zero. */
  const unmeasured = serving.error
    ? formatError(serving.error, "metrics unavailable")
    : (serving.data?.unavailable ?? null)

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Endpoints"
        description="declared · the stable public name clients call"
        actions={
          <Button size="lg" onClick={() => setCreating(true)}>
            New endpoint
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile label="Endpoints" value={String(total)} />
          <StatTile label="Enabled" value={String(enabled)} tone="ok" />
          {/* The gateway's own totals, not a sum over this page: adding up a
              page of endpoints gives a total that moves when somebody pages. */}
          <StatTile
            label="Requests / sec"
            value={formatMetric(requestsPerSec, "per_second")}
          />
          <StatTile label="p50 latency" value={formatMetric(p50, "seconds")} />
        </div>

        {unmeasured && (
          <Card size="sm" className="px-3 py-2.5">
            <p className="font-mono text-2xs text-muted-foreground/60">
              {unmeasured}
            </p>
          </Card>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Project"
            value={search.project ?? ALL}
            onChange={(value) =>
              navigate({
                search: (prev) => ({
                  ...prev,
                  project: value === ALL ? undefined : value,
                }),
              })
            }
            options={(projects ?? []).map((project) => ({
              value: project.id,
              label: project.slug,
            }))}
          />
        </div>

        <DataTable
          columns={columns(setEditing, setDoomed, resolve, read)}
          list={list}
          rowKey={(endpoint) => endpoint.id}
          empty="No endpoints published."
        />
      </div>

      {creating && (
        <EndpointForm
          open
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
      {editing && (
        <EndpointForm
          key={editing.revision}
          open
          endpoint={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="endpoint"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="endpoint"
        name={doomed?.name ?? ""}
        note="Clients calling this name start failing immediately."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Endpoint deleted", { description: doomed.name })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete endpoint")),
          })
        }
      />
    </div>
  )
}
