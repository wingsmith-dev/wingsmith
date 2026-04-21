import { MetricsSection } from "@/components/metrics/metrics-section"
import { StatusLabel } from "@/components/layout/status-dot"
import { Card } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { CLUSTER_PHASE_TONE, humanize } from "@/modules/clusters/display"
import type { Cluster } from "@/modules/clusters/types"
import {
  useClusterDeployments,
  useClusterDevices,
  useClusterEvents,
  useClusterNodes,
  useClusterOpenOperations,
  useClusterPools,
} from "@/modules/overview/queries"
import {
  deriveClusterActionables,
  summariseDevices,
  summariseNodes,
} from "@/modules/overview/summary"
import type { Deployment } from "@/modules/demand/types"
import type { Pool } from "@/modules/supply/types"
import { IconCheck, IconX } from "@tabler/icons-react"
import { Link } from "@tanstack/react-router"

import { Actionables, type SourceFailure } from "./Actionables"
import { ClusterEvents } from "./ClusterEvents"
import { ClusterIdentity } from "./Glance"
import { Section, SectionEmpty } from "./Section"

function StatTile({
  label,
  value,
  tone,
}: {
  label: string
  value: React.ReactNode
  tone?: "ok" | "warn"
}) {
  return (
    <Card size="sm" className="gap-1.5 px-4 py-3.5">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </span>
      <span
        className={
          "font-heading text-2xl leading-none font-semibold tabular-nums" +
          (tone === "ok"
            ? " text-emerald-500"
            : tone === "warn"
              ? " text-amber-500"
              : "")
        }
      >
        {value}
      </span>
    </Card>
  )
}

/** GPU allocation bar, warm as it fills. */
function UtilBar({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.round((used / total) * 100) : 0
  return (
    <div className="flex items-center gap-2">
      <Progress value={pct} className="h-1.5 min-w-14 flex-1" />
      <span className="w-14 shrink-0 text-right font-mono text-2xs text-muted-foreground">
        {used}/{total}
      </span>
    </div>
  )
}

function Th({ children }: React.PropsWithChildren) {
  return (
    <th className="px-3 py-2 text-left font-mono text-2xs font-semibold tracking-wider whitespace-nowrap text-muted-foreground uppercase">
      {children}
    </th>
  )
}

function Td({
  children,
  className,
}: React.PropsWithChildren<{ className?: string }>) {
  return (
    <td className={"px-3 py-2.5 align-middle whitespace-nowrap " + (className ?? "")}>
      {children}
    </td>
  )
}

/**
 * Declared node pools, side by side with their observed membership. GPUs
 * "allocated" is derived as the healthy devices a pool no longer offers as
 * allocatable — the same reads the pool page counts from.
 */
function NodePoolsTable({
  clusterId,
  pools,
}: {
  clusterId: string
  pools: { data: Pool[] | undefined; isLoading: boolean; error: unknown }
}) {
  const rows = pools.data ?? []
  return (
    <Section
      title="Node pools"
      description={`${rows.length} pools`}
      link={{
        props: { to: "/pools", search: { cluster: clusterId } },
        label: "All pools",
      }}
      isLoading={pools.isLoading}
      error={pools.error}
      errorMessage="Failed to load pools"
    >
      {rows.length === 0 ? (
        <SectionEmpty>No pools declared on this cluster.</SectionEmpty>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b">
              <Th>Pool</Th>
              <Th>GPU model</Th>
              <Th>Nodes</Th>
              <Th>GPUs</Th>
              <Th>Allocated</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((pool) => {
              const m = pool.membership
              const allocated = m.devices_healthy - m.devices_allocatable
              return (
                <tr key={pool.id} className="border-b last:border-0">
                  <Td>
                    <Link
                      to="/pools/$poolId"
                      params={{ poolId: pool.id }}
                      className="hover:underline"
                    >
                      {pool.name}
                    </Link>
                  </Td>
                  <Td className="font-mono text-2xs text-muted-foreground">
                    {pool.device_class}
                  </Td>
                  <Td className="font-mono tabular-nums">
                    {m.nodes_ready}/{m.nodes_total}
                  </Td>
                  <Td className="font-mono tabular-nums">{m.devices_total}</Td>
                  <Td className="min-w-40">
                    <UtilBar
                      used={Math.max(allocated, 0)}
                      total={m.devices_total}
                    />
                  </Td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Section>
  )
}

/**
 * Deployments running on this cluster. Recipe, pool and GPU columns the design
 * calls for are workload facts, not deployment facts, and are left for a future
 * expansion of the share object.
 */
function DeploymentsTable({
  clusterId,
  deployments,
}: {
  clusterId: string
  deployments: {
    data: Deployment[] | undefined
    isLoading: boolean
    error: unknown
  }
}) {
  const rows = deployments.data ?? []
  const replicas = rows.reduce(
    (sum, d) => sum + d.replicas.ready,
    0
  )
  const desired = rows.reduce((sum, d) => sum + d.desired_replicas, 0)
  return (
    <Section
      title="Deployments on this cluster"
      description={`${rows.length} deployments · ${replicas}/${desired} replicas`}
      link={{
        props: { to: "/deployments", search: { cluster: clusterId } },
        label: "All deployments",
      }}
      isLoading={deployments.isLoading}
      error={deployments.error}
      errorMessage="Failed to load deployments"
    >
      {rows.length === 0 ? (
        <SectionEmpty>No deployments on this cluster.</SectionEmpty>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b">
              <Th>Deployment</Th>
              <Th>Workload</Th>
              {/* TODO-API recipe/pool/GPU count are not on the deployment share */}
              <Th>Recipe</Th>
              <Th>Replicas</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => {
              const short = d.replicas.ready < d.desired_replicas
              return (
                <tr key={d.id} className="border-b last:border-0">
                  <Td>
                    <Link
                      to="/deployments/$deploymentId"
                      params={{ deploymentId: d.id }}
                      className="hover:underline"
                    >
                      {d.name}
                    </Link>
                  </Td>
                  <Td>
                    <Link
                      to="/workloads/$workloadId"
                      params={{ workloadId: d.workload_id }}
                      className="hover:underline"
                    >
                      {d.workload_name}
                    </Link>
                  </Td>
                  {/* TODO-API recipe name is not carried on the deployment */}
                  <Td className="font-mono text-2xs text-muted-foreground/40">
                    —
                  </Td>
                  <Td className="font-mono tabular-nums">
                    {d.replicas.ready}/{d.desired_replicas}
                  </Td>
                  <Td>
                    <StatusLabel tone={short ? "warn" : "ok"}>
                      {humanize(d.admission_phase)}
                    </StatusLabel>
                  </Td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Section>
  )
}

interface HeadroomTotal {
  label: string
  value: string
  tone?: "warn"
}

/**
 * Free capacity and how it is shaped. GPUs free is the drained allocatable
 * count; the block-shape figures the design leans on (largest contiguous block,
 * whole nodes free, VRAM free) are not yet computed by the plane.
 */
function HeadroomPanel({
  free,
  total,
}: {
  free: number
  total: number
}) {
  // TODO-API largest-block / whole-nodes-free / VRAM-free are not exposed yet
  const totals: HeadroomTotal[] = [
    { label: "GPUs free", value: String(free) },
    { label: "of fleet", value: `${free}/${total}` },
    { label: "largest block", value: "—" },
    { label: "whole nodes free", value: "—", tone: "warn" },
  ]
  return (
    <Card className="gap-0 border-primary/30 p-0">
      <div className="flex flex-wrap items-baseline gap-3 border-b px-4 py-3">
        <h2 className="font-heading text-sm font-medium">Headroom</h2>
        <p className="font-mono text-2xs text-muted-foreground/60">
          free capacity and how it&apos;s shaped
        </p>
      </div>
      <div className="grid grid-cols-2 gap-4 p-4 sm:grid-cols-4">
        {totals.map((t) => (
          <div key={t.label} className="flex flex-col gap-1">
            <span
              className={
                "font-heading text-2xl leading-none font-semibold tabular-nums" +
                (t.tone === "warn" ? " text-amber-500" : "")
              }
            >
              {t.value}
            </span>
            <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
              {t.label}
            </span>
          </div>
        ))}
      </div>
    </Card>
  )
}

interface FitRow {
  recipe: string
  shape: string
  pool: string
  fits: number
  limitedBy: string | null
}

/**
 * Which current recipe shapes fit the free capacity. Placement fit is not yet
 * surfaced by the plane, so the rows below are illustrative.
 */
function WhatFitsPanel() {
  // TODO-API placement fit (recipe shape vs free capacity) is not exposed yet
  const rows: FitRow[] = [
    {
      recipe: "vllm-h100-tp4",
      shape: "4× H100 · 320 GB",
      pool: "h100-sxm-8x",
      fits: 0,
      limitedBy: "largest free block is 1 GPU",
    },
    {
      recipe: "sglang-a100-tp2",
      shape: "2× A100 · 160 GB",
      pool: "a100-sxm-8x",
      fits: 3,
      limitedBy: null,
    },
    {
      recipe: "tei-l40s-1x",
      shape: "1× L40S · 48 GB",
      pool: "l40s-4x",
      fits: 8,
      limitedBy: null,
    },
  ]
  return (
    <Section
      title="What fits here"
      description="current recipe shapes against free capacity"
      isLoading={false}
      error={null}
      errorMessage="Failed to load fit"
    >
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b">
            <Th>Recipe</Th>
            <Th>Shape</Th>
            <Th>Pool</Th>
            <Th>Fits</Th>
            <Th>Limited by</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.recipe} className="border-b last:border-0">
              <Td>{r.recipe}</Td>
              <Td className="font-mono text-2xs text-muted-foreground">
                {r.shape}
              </Td>
              <Td className="font-mono text-2xs text-muted-foreground">
                {r.pool}
              </Td>
              <Td>
                <span
                  className={
                    "inline-flex items-center gap-1 font-mono text-2xs " +
                    (r.fits > 0
                      ? "text-emerald-500"
                      : "text-muted-foreground/50")
                  }
                >
                  {r.fits > 0 ? (
                    <IconCheck className="size-3.5" />
                  ) : (
                    <IconX className="size-3.5" />
                  )}
                  {r.fits} {r.fits === 1 ? "replica" : "replicas"}
                </span>
              </Td>
              <Td className="text-2xs text-muted-foreground">
                {r.limitedBy ?? <span className="text-muted-foreground/40">—</span>}
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  )
}

/**
 * Is this cluster healthy, and is there anything to do about it — in that
 * order. The actionable list is derived from the same drained reads the counts
 * below it are, so a row and a tile can never disagree.
 */
export function ClusterOverviewTab({ cluster }: { cluster: Cluster }) {
  const pools = useClusterPools(cluster.id)
  const nodes = useClusterNodes(cluster.id)
  const devices = useClusterDevices(cluster.id)
  const deployments = useClusterDeployments(cluster.id)
  const operations = useClusterOpenOperations(cluster.id)
  const events = useClusterEvents(cluster.id)

  const actionables = deriveClusterActionables({
    cluster,
    pools: pools.data,
    nodes: nodes.data,
    devices: devices.data,
    deployments: deployments.data,
    operations: operations.data,
  })

  const failures: SourceFailure[] = (
    [
      ["Pools", pools.error],
      ["Nodes", nodes.error],
      ["Devices", devices.error],
      ["Deployments", deployments.error],
      ["Operations", operations.error],
    ] as const
  )
    .filter(([, error]) => !!error)
    .map(([label, error]) => ({ label, error }))

  const nodeSummary = nodes.data && summariseNodes(nodes.data)
  const deviceSummary = devices.data && summariseDevices(devices.data)
  const replicasReady =
    deployments.data?.reduce((sum, d) => sum + d.replicas.ready, 0) ?? 0
  const replicasDesired =
    deployments.data?.reduce((sum, d) => sum + d.desired_replicas, 0) ?? 0
  // TODO-API a first-class allocated-GPU count is not exposed; derived from devices
  const gpusAllocated = deviceSummary
    ? deviceSummary.total - deviceSummary.allocatable
    : 0

  return (
    <div className="space-y-4">
      <Actionables
        actionables={actionables}
        isLoading={
          pools.isLoading ||
          nodes.isLoading ||
          devices.isLoading ||
          deployments.isLoading ||
          operations.isLoading
        }
        failures={failures}
      />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
        <StatTile
          label="Status"
          value={humanize(cluster.phase)}
          tone={
            (CLUSTER_PHASE_TONE[cluster.phase] ?? "neutral") === "ok"
              ? "ok"
              : undefined
          }
        />
        <StatTile
          label="Nodes ready"
          value={
            nodeSummary ? `${nodeSummary.ready}/${nodeSummary.total}` : "—"
          }
          tone={
            nodeSummary && nodeSummary.notReady > 0 ? "warn" : undefined
          }
        />
        <StatTile
          label="GPUs allocated"
          value={
            deviceSummary ? `${gpusAllocated}/${deviceSummary.total}` : "—"
          }
        />
        <StatTile
          label="Deployments"
          value={deployments.data?.length ?? "—"}
        />
        <StatTile
          label="Replicas"
          value={`${replicasReady}/${replicasDesired}`}
          tone={replicasReady < replicasDesired ? "warn" : undefined}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <HeadroomPanel
            free={deviceSummary?.allocatable ?? 0}
            total={deviceSummary?.total ?? 0}
          />
          <NodePoolsTable clusterId={cluster.id} pools={pools} />
          <DeploymentsTable clusterId={cluster.id} deployments={deployments} />
          <WhatFitsPanel />
        </div>

        <div className="space-y-4">
          <ClusterIdentity cluster={cluster} />

          <MetricsSection scope="cluster" refId={cluster.id} />

          <ClusterEvents
            clusterId={cluster.id}
            events={events.items}
            isLoading={events.isLoading}
            error={events.error}
          />
        </div>
      </div>
    </div>
  )
}
