import type { Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { DiscardBadge } from "@/components/benchmarks/discard"
import { Badge } from "@/components/ui/badge"
import { formatRelative, shortenId } from "@/lib/format"
import {
  asStatBlock,
  benchmarkPhaseTone,
  formatStat,
  loadSummary,
} from "@/modules/benchmarks/display"
import { runKindMeta } from "@/modules/benchmarks/kinds"
import type { BenchmarkRun } from "@/modules/benchmarks/types"
import { Link } from "@tanstack/react-router"

/**
 * The kind's own headline number, read out of the harness's summary if it is
 * there.
 *
 * A guess by construction: the plane stores whatever shape the tool emitted and
 * promises nothing about it. The key comes from the kind instead of being
 * hardcoded to TTFT, because a conformance run has no time-to-first-token and a
 * column headed `TTFT p50` full of dashes reads as a broken measurement rather
 * than as a question that was never asked.
 */
function headline(run: BenchmarkRun): string {
  const meta = runKindMeta(run.harness)
  if (!meta.headline || !run.summary) return "—"
  const block = asStatBlock(run.summary[meta.headline.key])
  if (!block) return "—"
  const value = block.p50 ?? block.avg
  return value === undefined ? "—" : formatStat(value)
}

/**
 * What the run was pointed at.
 *
 * A run carries an endpoint or a deployment and never a workload: a
 * workload-level ask expands into one run per placement, so the workload is a
 * fact about the request and the runs each name the thing they actually hit.
 * The request link in the row is how the wider claim is reached.
 */
function targetCell(run: BenchmarkRun) {
  if (run.endpoint_id)
    return (
      <Link
        to="/endpoints/$endpointId"
        params={{ endpointId: run.endpoint_id }}
        className="font-mono text-2xs hover:underline"
      >
        <span className="text-muted-foreground">endpoint </span>
        {shortenId(run.endpoint_id)}
      </Link>
    )
  if (run.deployment_id)
    return (
      <Link
        to="/deployments/$deploymentId"
        params={{ deploymentId: run.deployment_id }}
        className="font-mono text-2xs hover:underline"
      >
        <span className="text-muted-foreground">deployment </span>
        {shortenId(run.deployment_id)}
      </Link>
    )
  return (
    <span className="font-mono text-2xs">
      <Dash>{null}</Dash>
    </span>
  )
}

/**
 * The fleet-wide run list: type, target, status.
 *
 * Narrower than `benchmarkColumns`. That one is embedded under an
 * object that already supplies the context: you are on a workload, so every
 * row's level and target are known and the interesting columns are the numbers.
 * Here nothing is known, so the three identifying facts come first and exactly
 * one number follows, chosen by the kind.
 */
export function runColumns(): Column<BenchmarkRun>[] {
  return [
    {
      key: "type",
      header: "Type",
      cell: (run) => {
        const meta = runKindMeta(run.harness)
        return (
          <div className="flex items-center gap-2">
            <StatusLabel tone={meta.tone}>{meta.label}</StatusLabel>
          </div>
        )
      },
    },
    {
      key: "run",
      header: "Run",
      cell: (run) => (
        <ResourceCell kind="benchmark">
          <Link
            to="/workbench/$runId"
            params={{ runId: run.id }}
            className="block font-mono text-xs hover:underline"
          >
            {run.harness}
            {run.harness_version && (
              <span className="text-muted-foreground">
                {" "}
                {run.harness_version}
              </span>
            )}
            <Id value={shortenId(run.id)} className="ml-2" />
          </Link>
          {run.discard && <DiscardBadge />}
        </ResourceCell>
      ),
    },
    {
      key: "target",
      header: "Target",
      cell: targetCell,
    },
    {
      key: "phase",
      header: "Status",
      cell: (run) => (
        <StatusLabel
          tone={benchmarkPhaseTone(run.phase)}
          pulse={run.phase === "running"}
        >
          {run.phase}
        </StatusLabel>
      ),
    },
    {
      key: "headline",
      header: "Result",
      align: "right",
      cell: (run) => (
        <span className="font-mono text-2xs">{headline(run)}</span>
      ),
    },
    {
      key: "load",
      header: "Shape",
      secondary: true,
      cell: (run) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>{loadSummary(run.parameters) || null}</Dash>
        </span>
      ),
    },
    {
      key: "request",
      header: "Requested by",
      secondary: true,
      cell: (run) =>
        run.request_id ? (
          <Link
            to="/workbench/requests/$requestId"
            params={{ requestId: run.request_id }}
            className="font-mono text-2xs hover:underline"
          >
            {shortenId(run.request_id)}
          </Link>
        ) : (
          <Badge variant="outline" className="font-mono text-2xs">
            direct
          </Badge>
        ),
    },
    {
      key: "when",
      header: "Started",
      align: "right",
      cell: (run) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(run.started_at ?? run.created_at)}
        </span>
      ),
    },
  ]
}
