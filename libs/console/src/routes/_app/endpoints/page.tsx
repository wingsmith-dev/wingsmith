import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusDot, type StatusTone } from "@/components/layout/status-dot"
import { Sparkline } from "@/components/metrics/sparkline"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { useProjectOptions } from "@/modules/binding/queries"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { formatError, shortenId } from "@/lib/format"
import { useDeleteEndpoint, useEndpoints } from "@/modules/demand/queries"
import type { Endpoint } from "@/modules/demand/types"
import type { MetricSeriesPoint } from "@/modules/metrics/types"
import { EndpointForm } from "./EndpointForm"
import { useState } from "react"
import { toast } from "sonner"
import { IconArrowUpRight } from "@tabler/icons-react"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

/**
 * A stable pseudo-random seed from an id, so every mocked telemetry figure on a
 * row stays put across renders rather than flickering each paint.
 */
function seed(id: string): number {
  let hash = 0
  for (let i = 0; i < id.length; i++) {
    hash = (hash * 31 + id.charCodeAt(i)) | 0
  }
  return Math.abs(hash)
}

function pick<T>(id: string, salt: number, options: readonly T[]): T {
  return options[(seed(id) + salt) % options.length]
}

function trail(values: number[]): MetricSeriesPoint[] {
  return values.map((value, at) => ({ at, value }))
}

/** A short climbing/spiky series shaped by the seed, purely for sparkline form. */
function mockTrail(id: string, salt: number): MetricSeriesPoint[] {
  const base = seed(`${id}:${salt}`)
  return trail(
    Array.from({ length: 8 }, (_, i) => 10 + ((base >> i) % 7) + i * ((base % 3) - 1))
  )
}

const REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"] as const
const QUANTIZATIONS = ["fp8 · awq", "fp16", "int8", "fp8"] as const
const AUTH_LABELS: Record<string, string> = {
  none: "public",
  api_key: "api key",
  mtls: "mTLS",
}

interface Health {
  tone: StatusTone
  label: string
  availability: string
  marks: ("up" | "warn" | "down")[]
}

/**
 * Endpoint health, availability and traffic are data-plane observed state that
 * reaches the observability stack directly and never the plane object — mocked
 * here, deterministic per endpoint, so the designed status column stays real to
 * scan. Only `enabled` is a true plane fact and it pins a disabled endpoint to
 * "Provisioning".
 */
function mockHealth(endpoint: Endpoint): Health {
  if (!endpoint.enabled) {
    return {
      tone: "neutral",
      label: "Provisioning",
      availability: "no data yet",
      marks: Array.from({ length: 14 }, () => "down"),
    }
  }
  const roll = seed(endpoint.id) % 10
  const tone: StatusTone = roll < 6 ? "ok" : roll < 9 ? "warn" : "danger"
  const label = tone === "ok" ? "Healthy" : tone === "warn" ? "Degraded" : "Down"
  const availability =
    tone === "ok" ? "99.95%" : tone === "warn" ? "98.10%" : "94.20%"
  const marks: ("up" | "warn" | "down")[] = Array.from({ length: 14 }, (_, i) => {
    if (tone === "ok") return i === (roll % 14) ? "warn" : "up"
    if (tone === "warn") return i > 8 ? "warn" : "up"
    return i > 11 ? "down" : i === 11 ? "warn" : "up"
  })
  return { tone, label, availability, marks }
}

/** The 14-day availability strip, one cell per day. */
function AvailabilityBar({ marks }: { marks: ("up" | "warn" | "down")[] }) {
  return (
    <div className="flex items-end gap-0.5">
      {marks.map((mark, i) => (
        <span
          key={i}
          className={cn(
            "h-2.5 w-[3px] rounded-[1px]",
            mark === "up" && "bg-emerald-500",
            mark === "warn" && "bg-amber-500",
            mark === "down" && "bg-destructive"
          )}
        />
      ))}
    </div>
  )
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

function columns(
  onEdit: (endpoint: Endpoint) => void,
  onDelete: (endpoint: Endpoint) => void
): Column<Endpoint>[] {
  return [
    {
      key: "status",
      header: (
        <span>
          Status{" "}
          <span className="text-muted-foreground/50 normal-case">/ 14d</span>
        </span>
      ),
      cell: (endpoint) => {
        const health = mockHealth(endpoint)
        return (
          <div className="flex flex-col gap-1.5">
            <span className="inline-flex items-center gap-2">
              <StatusDot tone={health.tone} />
              <span className="text-2xs text-muted-foreground">
                {health.label}
              </span>
            </span>
            {/* TODO-API availability history is data-plane telemetry, mocked. */}
            <div className="flex items-center gap-1.5">
              <AvailabilityBar marks={health.marks} />
              <span className="font-mono text-2xs text-muted-foreground/60">
                {health.availability}
              </span>
            </div>
          </div>
        )
      },
    },
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
            <span className="font-mono text-xs">{endpoint.name}</span>
            <Id value={shortenId(endpoint.id)} className="ml-2" />
          </Link>
          {/* TODO-API the public URL is a gateway fact, not on the object. */}
          <div className="font-mono text-2xs text-muted-foreground/60">
            hangar.example.com/v1/{endpoint.name}
          </div>
        </ResourceCell>
      ),
    },
    {
      key: "workload",
      header: "Workload",
      secondary: true,
      cell: (endpoint) => (
        // TODO-API the backing workload is reached through a target's
        // deployment, not carried on the endpoint; a plausible name is mocked.
        <span className="inline-flex items-center gap-1 font-mono text-2xs text-muted-foreground">
          {endpoint.name}-svc
          <IconArrowUpRight className="size-3 text-muted-foreground/50" />
        </span>
      ),
    },
    {
      key: "model",
      header: "Model",
      secondary: true,
      cell: (endpoint) => (
        // TODO-API the served model is a recipe/artifact fact reached through
        // the deployment, not on the endpoint; mocked for the designed column.
        <div className="flex flex-col gap-0.5">
          <span className="text-xs">{endpoint.name}-model</span>
          <span className="font-mono text-2xs text-muted-foreground/60">
            {pick(endpoint.id, 3, QUANTIZATIONS)}
          </span>
        </div>
      ),
    },
    {
      key: "traffic",
      header: "Traffic",
      cell: (endpoint) => {
        const health = mockHealth(endpoint)
        if (health.tone === "neutral") return <Dash>{null}</Dash>
        return (
          // TODO-API requests/sec is data-plane telemetry, mocked.
          <div className="flex flex-col gap-1">
            <div className="w-14">
              <Sparkline
                points={mockTrail(endpoint.id, 1)}
                tone={health.tone === "ok" ? "primary" : "warn"}
              />
            </div>
            <span className="font-mono text-2xs tabular-nums">
              {(seed(endpoint.id) % 1200) + 40} req/s
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
        const health = mockHealth(endpoint)
        if (health.tone === "neutral" || health.tone === "danger")
          return <Dash>{null}</Dash>
        const p50 = (seed(endpoint.id) % 400) + 30
        return (
          // TODO-API latency percentiles are data-plane telemetry, mocked.
          <div className="flex flex-col gap-1">
            <div className="w-14">
              <Sparkline
                points={mockTrail(endpoint.id, 2)}
                tone={health.tone === "ok" ? "primary" : "warn"}
              />
            </div>
            <span className="font-mono text-2xs tabular-nums">
              {p50}ms / {p50 * 2 + 40}ms
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
      cell: (endpoint) => (
        // TODO-API region affinity is derived from a target's deployment, not
        // on the endpoint; mocked so the column reads.
        <span className="font-mono text-2xs text-muted-foreground">
          {pick(endpoint.id, 5, REGIONS)}
        </span>
      ),
    },
    {
      key: "auth",
      header: "Auth",
      cell: (endpoint) => (
        <Badge variant="outline" className="font-mono text-2xs">
          {AUTH_LABELS[endpoint.surface?.auth_mode ?? ""] ??
            endpoint.surface?.auth_mode ??
            "—"}
        </Badge>
      ),
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
  const remove = useDeleteEndpoint()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Endpoint | undefined>()
  const [doomed, setDoomed] = useState<Endpoint | undefined>()

  const endpoints = list.items ?? []
  const total = endpoints.length
  const healthy = endpoints.filter((e) => e.enabled).length
  // TODO-API aggregate serving telemetry (requests/sec, p50) is data-plane. */
  const requestsPerSec = endpoints.reduce(
    (sum, e) => sum + (e.enabled ? (seed(e.id) % 1200) + 40 : 0),
    0
  )

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Endpoints"
        description="declared · the stable public name clients call"
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            New endpoint
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile label="Endpoints" value={String(total)} />
          <StatTile label="Healthy" value={String(healthy)} tone="ok" />
          {/* TODO-API requests/sec is data-plane telemetry, mocked. */}
          <StatTile
            label="Requests / sec"
            value={requestsPerSec.toLocaleString()}
          />
          {/* TODO-API p50 latency is data-plane telemetry, mocked. */}
          <StatTile label="p50 latency" value="128ms" />
        </div>

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
          columns={columns(setEditing, setDoomed)}
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
