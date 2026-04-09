import type { Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { formatRelative } from "@/lib/format"
import {
  asStatBlock,
  benchmarkPhaseTone,
  formatStat,
  loadSummary,
} from "@/modules/benchmarks/display"
import type { BenchmarkRun } from "@/modules/benchmarks/types"
import { Link } from "@tanstack/react-router"

/**
 * Two headline numbers, read out of the harness's own summary if they happen to
 * be there.
 *
 * A guess by construction, and it has to be: the plane stores whatever shape the
 * tool emitted and promises nothing about it. A run whose summary does not carry
 * these under these names shows a dash and is still perfectly readable on its
 * detail page — which is the right trade for not pretending to a schema.
 */
function headline(run: BenchmarkRun, key: string): string {
  const summary = run.summary
  if (!summary) return "—"
  const block = asStatBlock(summary[key])
  if (!block) return "—"
  const value = block.p50 ?? block.avg
  return value === undefined ? "—" : formatStat(value)
}

export function benchmarkColumns(
  options: { showDeployment?: boolean } = {}
): Column<BenchmarkRun>[] {
  const columns: Column<BenchmarkRun>[] = [
    {
      key: "run",
      header: "Run",
      cell: (run) => (
        <ResourceCell kind="benchmark">
          <Link
            to="/benchmarks/$benchmarkId"
            params={{ benchmarkId: run.id }}
            className="block font-mono text-xs hover:underline"
          >
            {run.harness}
            {run.harness_version && (
              <span className="text-muted-foreground/60">
                {" "}
                {run.harness_version}
              </span>
            )}
            <Id value={run.id} className="ml-2" />
          </Link>
        </ResourceCell>
      ),
    },
  ]

  if (options.showDeployment) {
    columns.push({
      key: "deployment",
      header: "Deployment",
      secondary: true,
      cell: (run) => (
        <Link
          to="/deployments/$deploymentId"
          params={{ deploymentId: run.deployment_id }}
          className="font-mono text-2xs hover:underline"
        >
          {run.deployment_id}
        </Link>
      ),
    })
  }

  columns.push(
    {
      key: "phase",
      header: "Phase",
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
      key: "load",
      header: "Load",
      cell: (run) => (
        <span className="font-mono text-2xs text-muted-foreground">
          <Dash>{loadSummary(run.parameters) || null}</Dash>
        </span>
      ),
    },
    {
      key: "ttft",
      header: "TTFT p50",
      align: "right",
      cell: (run) => (
        <span className="font-mono text-2xs">
          {headline(run, "time_to_first_token")}
        </span>
      ),
    },
    {
      key: "itl",
      header: "ITL p50",
      align: "right",
      secondary: true,
      cell: (run) => (
        <span className="font-mono text-2xs">
          {headline(run, "inter_token_latency")}
        </span>
      ),
    },
    {
      key: "generation",
      header: "Generation",
      secondary: true,
      cell: (run) => (
        <Badge variant="outline" className="font-mono text-2xs">
          gen {run.provenance.deployment_generation}
        </Badge>
      ),
    },
    {
      key: "when",
      header: "Started",
      align: "right",
      cell: (run) => (
        <span className="font-mono text-2xs text-muted-foreground/70">
          {formatRelative(run.started_at ?? run.created_at)}
        </span>
      ),
    }
  )

  return columns
}
