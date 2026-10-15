import { DiscardBadge } from "@/components/benchmarks/discard"
import { LevelBadge } from "@/components/benchmarks/level"
import { BenchmarkSummaryView } from "@/components/benchmarks/summary-view"
import { VerdictPanel } from "@/components/benchmarks/verdict"
import { AgentRun } from "@/components/workbench/agent"
import { ConformanceRun } from "@/components/workbench/conformance"
import { EvaluationRun } from "@/components/workbench/evaluation"
import { MockNotice } from "@/components/workbench/mock-notice"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { benchmarkPhaseTone, LEVEL_CLAIM } from "@/modules/benchmarks/display"
import { runKind, runKindMeta } from "@/modules/benchmarks/kinds"
import { isMockId, mockRequest, mockRun } from "@/modules/benchmarks/mock"
import {
  useBenchmark,
  useBenchmarkRequest,
  useBenchmarkVerdict,
} from "@/modules/benchmarks/queries"
import { isOpen, type BenchmarkRun } from "@/modules/benchmarks/types"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"

/**
 * One run, and the ask it came from.
 *
 * Everything on this page is observed and none of it is editable. The level is
 * read off the request rather than guessed from which ids the run happens to
 * carry: a deployment-level and a workload-level run both name a deployment,
 * and the difference between them is the whole claim.
 */
export function BenchmarkDetailPage() {
  const { runId } = useParams({ from: "/_app/workbench/$runId" })
  const mock = isMockId(runId)

  /*
    A mock id resolves locally and asks the plane nothing. The alternative — a
    fetch that 404s and an error screen behind the row somebody just clicked —
    makes the switch look broken rather than deliberate.
  */
  const query = useBenchmark(mock ? "" : runId)
  const run = mock ? mockRun(runId) : query.data
  const isLoading = mock ? false : query.isLoading
  const error = mock ? null : query.error

  const requestQuery = useBenchmarkRequest(
    mock || !run?.request_id ? "" : run.request_id
  )
  const request = mock ? mockRequest(run!.request_id!) : requestQuery.data

  const { data: verdict } = useBenchmarkVerdict(
    run && !mock && !isOpen(run.phase) ? run.id : undefined
  )

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Benchmark run" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load benchmark run")}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={run ? `${run.harness} run` : "Benchmark run"}
        description={run?.id}
        readActions={
          <div className="flex gap-2">
            {run?.request_id && (
              <Button asChild variant="outline">
                <Link
                  to="/workbench/requests/$requestId"
                  params={{ requestId: run.request_id }}
                >
                  Request
                </Link>
              </Button>
            )}
            {run?.operation_id && (
              <Button asChild variant="outline">
                <Link
                  to="/operations/$operationId"
                  params={{ operationId: run.operation_id }}
                >
                  Operation
                </Link>
              </Button>
            )}
            {run?.deployment_id && (
              <Button asChild variant="outline">
                <Link
                  to="/deployments/$deploymentId"
                  params={{ deploymentId: run.deployment_id }}
                >
                  Deployment
                </Link>
              </Button>
            )}
            {run?.endpoint_id && (
              <Button asChild variant="outline">
                <Link
                  to="/endpoints/$endpointId"
                  params={{ endpointId: run.endpoint_id }}
                >
                  Endpoint
                </Link>
              </Button>
            )}
            <Button asChild variant="outline">
              <Link to="/workbench">
                <IconArrowLeft className="size-4" />
                All benchmarks
              </Link>
            </Button>
          </div>
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}
        {mock && (
          <MockNotice what="This whole run is invented, reached from the list's Mock switch." />
        )}

        {run && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              {request && <LevelBadge level={request.level} />}
              <StatusLabel
                tone={benchmarkPhaseTone(run.phase)}
                pulse={run.phase === "running"}
              >
                {run.phase}
              </StatusLabel>
              <span className="font-mono text-2xs text-muted-foreground">
                {run.harness}
                {run.harness_version ? ` ${run.harness_version}` : ""}
              </span>
              {run.discard && <DiscardBadge />}
            </div>

            {request && (
              <p className="text-xs text-muted-foreground">
                Step {run.step_index} of a {request.mode} request measuring{" "}
                {LEVEL_CLAIM[request.level]}.
              </p>
            )}

            {run.error && (
              <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 font-mono text-2xs text-destructive">
                {run.error}
              </div>
            )}

            <div className="grid gap-4 lg:grid-cols-3">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Run</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Harness">{run.harness}</Field>
                    <Field label="Version">
                      <Dash>{run.harness_version}</Dash>
                    </Field>
                    <Field label="Step">#{run.step_index}</Field>
                    <Field label="Vantage">
                      <Dash>{run.vantage}</Dash>
                    </Field>
                    <Field label="Requested by">{run.requested_by}</Field>
                    <Field label="Counted">
                      {run.discard ? "no — discarded warm-up" : "yes"}
                    </Field>
                    <Field label="Image" className="sm:col-span-2">
                      <Dash>{run.image}</Dash>
                    </Field>
                    <Field label="Started">
                      {formatRelative(run.started_at)}
                    </Field>
                    <Field label="Finished">
                      {formatRelative(run.finished_at)}
                    </Field>
                    <Field label="Created">
                      {formatAbsolute(run.created_at)}
                    </Field>
                    <Field label="Reason" className="sm:col-span-2">
                      <Dash>{run.reason}</Dash>
                    </Field>
                    <Field label="Raw records" className="sm:col-span-2">
                      <Dash>{run.artifacts_uri}</Dash>
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Load</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    {Object.entries(run.parameters).map(([key, value]) => (
                      <Field key={key} label={key}>
                        <Dash>{value === "" ? null : String(value)}</Dash>
                      </Field>
                    ))}
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">What was measured</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <FieldGrid>
                    <Field label="Model" className="sm:col-span-2">
                      <Dash>{run.provenance.model}</Dash>
                    </Field>
                    <Field label="Deployment">
                      {run.deployment_id ? (
                        <Link
                          to="/deployments/$deploymentId"
                          params={{ deploymentId: run.deployment_id }}
                          className="hover:underline"
                        >
                          {run.deployment_id}
                        </Link>
                      ) : (
                        <Dash>{null}</Dash>
                      )}
                    </Field>
                    <Field label="Generation">
                      {run.provenance.deployment_generation}
                    </Field>
                    <Field label="Serving digest">
                      <span className="font-mono text-2xs break-all">
                        <Dash>{run.provenance.serving_digest}</Dash>
                      </span>
                    </Field>
                  </FieldGrid>
                  {run.endpoint_id && (
                    <p className="text-2xs text-muted-foreground">
                      An endpoint-level run carries no snapshot. The request was
                      routed by weight across every enabled upstream, so no one
                      of them is what this measured.
                    </p>
                  )}
                  {Object.keys(run.provenance.engine_args).length > 0 && (
                    <pre className="overflow-x-auto rounded-lg border bg-muted/30 p-2 font-mono text-2xs">
                      {JSON.stringify(run.provenance.engine_args, null, 2)}
                    </pre>
                  )}
                  {Object.keys(run.provenance.serving).length > 0 && (
                    <pre className="overflow-x-auto rounded-lg border bg-muted/30 p-2 font-mono text-2xs">
                      {JSON.stringify(run.provenance.serving, null, 2)}
                    </pre>
                  )}
                </CardContent>
              </Card>
            </div>

            {verdict && <VerdictPanel verdict={verdict} title="Verdict" />}

            <div>
              <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
                Result · {runKindMeta(run.harness).label.toLowerCase()}
              </h2>
              <RunBody run={run} />
            </div>

            {run.log_tail && (
              <div>
                <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
                  Log tail
                </h2>
                <pre className="max-h-[28rem] overflow-auto rounded-lg border bg-muted/30 p-3 font-mono text-2xs whitespace-pre-wrap">
                  {run.log_tail}
                </pre>
                <p className="mt-1 text-2xs text-muted-foreground">
                  Tail-capped by the operator at ~64 KiB. The full run, when it
                  happens, lives under artifacts_uri — not here.
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

/**
 * The result, rendered as the kind that produced it.
 *
 * Everything above is a fact about a run — its harness, its parameters, what
 * was behind it when it went — and reads identically whatever was asked. This
 * is the half that does not: a percentile block, a claim ledger, a transcript
 * and a score table share no axis, and rendering all four through one generic
 * key-value dump is how a result page becomes something nobody opens.
 */
function RunBody({ run }: { run: BenchmarkRun }) {
  if (run.phase === "pending" || run.phase === "running")
    return (
      <div className="rounded-lg border bg-card p-6 text-center text-xs text-muted-foreground">
        The harness has not reported yet.
      </div>
    )

  switch (runKind(run.harness)) {
    case "conformance":
      return <ConformanceRun id={run.id} />
    case "agent":
      return <AgentRun id={run.id} />
    case "evaluation":
      return <EvaluationRun id={run.id} />
    default:
      return run.summary ? (
        <BenchmarkSummaryView summary={run.summary} />
      ) : (
        <div className="rounded-lg border bg-card p-6 text-center text-xs text-muted-foreground">
          This run produced no result.
        </div>
      )
  }
}
