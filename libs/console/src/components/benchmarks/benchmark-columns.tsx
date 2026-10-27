import type { Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { LevelBadge } from "@/components/benchmarks/level"
import { Badge } from "@/components/ui/badge"
import { DiscardBadge } from "@/components/benchmarks/discard"
import { formatRelative, shortenId } from "@/lib/format"
import {
  asStatBlock,
  benchmarkPhaseTone,
  formatStat,
  loadSummary,
} from "@/modules/benchmarks/display"
import type { BenchmarkLevel, BenchmarkRun } from "@/modules/benchmarks/types"
import { Link } from "@tanstack/react-router"

/**
 * Two headline numbers, read out of the harness's own summary if they happen to
 * be there.
 *
 * A guess by construction, and it has to be: the plane stores whatever shape the
 * tool emitted and promises nothing about it. A run whose summary does not carry
 * these under these names shows a dash and is still perfectly readable on its
 * detail page, which is the right trade for not pretending to a schema.
 */
function headline(run: BenchmarkRun, key: string): string {
  const summary = run.summary
  if (!summary) return "—"
  const block = asStatBlock(summary[key])
  if (!block) return "—"
  const value = block.p50 ?? block.avg
  return value === undefined ? "—" : formatStat(value)
}

/**
 * Columns over runs, which are observed and therefore carry no action column.
 *
 * There is no edit and no delete here and there never will be: a measurement is
 * not editable, and an affordance that suggested it was would be offering to
 * rewrite the record the table exists to be.
 *
 * `level` is passed in, not read off the run, because a run does not
 * carry one: the request does. A caller that knows which ask these rows came
 * from says so and gets the claim drawn on every row; a caller listing the
 * whole fleet does not, and shows the ids the run actually has instead.
 */
export function benchmarkColumns(
  options: {
    showDeployment?: boolean
    showRequest?: boolean
    showStep?: boolean
    level?: BenchmarkLevel
  } = {}
): Column<BenchmarkRun>[] {
  const columns: Column<BenchmarkRun>[] = [
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
  ]

  if (options.level) {
    columns.push({
      key: "level",
      header: "Level",
      cell: () => <LevelBadge level={options.level!} />,
    })
  }

  if (options.showStep) {
    columns.push({
      key: "step",
      header: "Step",
      align: "right",
      cell: (run) => (
        <span className="font-mono text-2xs text-muted-foreground">
          #{run.step_index}
        </span>
      ),
    })
  }

  if (options.showRequest) {
    columns.push({
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
          <span className="font-mono text-2xs text-muted-foreground">—</span>
        ),
    })
  }

  if (options.showDeployment) {
    columns.push({
      key: "deployment",
      header: "Measured",
      secondary: true,
      cell: (run) => {
        if (run.endpoint_id)
          return (
            <Link
              to="/endpoints/$endpointId"
              params={{ endpointId: run.endpoint_id }}
              className="font-mono text-2xs hover:underline"
            >
              {run.endpoint_id}
            </Link>
          )
        if (run.deployment_id)
          return (
            <Link
              to="/deployments/$deploymentId"
              params={{ deploymentId: run.deployment_id }}
              className="font-mono text-2xs hover:underline"
            >
              {run.deployment_id}
            </Link>
          )
        return (
          <span className="font-mono text-2xs">
            <Dash>{null}</Dash>
          </span>
        )
      },
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
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(run.started_at ?? run.created_at)}
        </span>
      ),
    }
  )

  return columns
}
