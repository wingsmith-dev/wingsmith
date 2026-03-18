import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { Id, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { formatRelative } from "@/lib/format"
import {
  CLUSTER_PHASES,
  CLUSTER_PHASE_TONE,
  clusterLabel,
  humanize,
  syncTone,
} from "@/modules/clusters/display"
import { useClusters } from "@/modules/clusters/queries"
import type { Cluster } from "@/modules/clusters/types"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

const columns: Column<Cluster>[] = [
  {
    key: "name",
    header: "Cluster",
    cell: (cluster) => (
      <Link
        to="/clusters/$clusterId"
        params={{ clusterId: cluster.id }}
        className="block"
      >
        <span className="font-medium">{clusterLabel(cluster)}</span>
        <Id value={cluster.name} className="ml-2" />
      </Link>
    ),
  },
  {
    key: "region",
    header: "Region",
    cell: (cluster) => (
      <span className="font-mono text-xs text-muted-foreground">
        {cluster.region}
        {cluster.zone && (
          <span className="text-muted-foreground/50"> · {cluster.zone}</span>
        )}
      </span>
    ),
  },
  {
    key: "phase",
    header: "Phase",
    cell: (cluster) => (
      <StatusLabel tone={CLUSTER_PHASE_TONE[cluster.phase] ?? "neutral"}>
        {humanize(cluster.phase)}
      </StatusLabel>
    ),
  },
  {
    key: "reachable",
    header: "Reachability",
    cell: (cluster) => (
      <StatusLabel tone={cluster.reachable ? "ok" : "danger"}>
        {cluster.reachable ? "reachable" : "unreachable"}
      </StatusLabel>
    ),
  },
  {
    key: "sync",
    header: "Sync",
    cell: (cluster) => (
      <StatusLabel
        tone={syncTone(cluster.sync.state)}
        pulse={cluster.sync.state === "connected"}
      >
        {humanize(cluster.sync.state)}
      </StatusLabel>
    ),
  },
  {
    key: "agent",
    header: "Agent",
    secondary: true,
    cell: (cluster) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {cluster.sync.agent_version ?? "—"}
      </span>
    ),
  },
  {
    key: "labels",
    header: "Labels",
    secondary: true,
    cell: (cluster) => <Labels labels={cluster.labels} />,
  },
  {
    key: "last_sync",
    header: "Last sync",
    align: "right",
    cell: (cluster) => (
      <span className="font-mono text-2xs text-muted-foreground/70">
        {formatRelative(cluster.sync.last_sync_at)}
      </span>
    ),
  },
]

/**
 * Phase and reachability are two columns and never one. A cluster that is
 * `active` and unreachable is a network problem; a `disabled` one is somebody's
 * decision, and an operator has to be able to tell them apart at a glance.
 */
export function ClustersPage() {
  const search = useSearch({ from: "/_app/clusters" })
  const navigate = useNavigate({ from: "/clusters" })
  const list = useClusters({ phase: search.phase, region: search.region })

  const regions = [...new Set((list.items ?? []).map((c) => c.region))].sort()

  const set = (key: "phase" | "region") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Clusters"
        description="declared enrolment · observed reachability and sync"
      />

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Phase"
            value={search.phase ?? ALL}
            onChange={set("phase")}
            options={CLUSTER_PHASES.map((phase) => ({ value: phase, label: phase }))}
          />
          <FilterSelect
            label="Region"
            value={search.region ?? ALL}
            onChange={set("region")}
            options={regions.map((region) => ({ value: region, label: region }))}
          />
        </div>

        <DataTable
          columns={columns}
          list={list}
          rowKey={(cluster) => cluster.id}
          empty="No clusters enrolled."
        />
      </div>
    </div>
  )
}
