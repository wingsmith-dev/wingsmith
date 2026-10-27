import { benchmarkColumns } from "@/components/benchmarks/benchmark-columns"
import { LevelBadge, TargetLink } from "@/components/benchmarks/level"
import { VerdictPanel } from "@/components/benchmarks/verdict"
import { AgentSuite } from "@/components/workbench/agent"
import { ConformanceSuite } from "@/components/workbench/conformance"
import { EvaluationSuite } from "@/components/workbench/evaluation"
import { PerformanceSuite } from "@/components/workbench/performance"
import { MockNotice } from "@/components/workbench/mock-notice"
import { DataTable } from "@/components/layout/data-table"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, shortenId } from "@/lib/format"
import {
  benchmarkPhaseTone,
  LEVEL_CLAIM,
  STEPPED_MODES,
} from "@/modules/benchmarks/display"
import { runKind } from "@/modules/benchmarks/kinds"
import {
  isMockId,
  mockRequest,
  mockRun,
  mockRunId,
} from "@/modules/benchmarks/mock"
import {
  useBenchmarkRequest,
  useRequestRuns,
  useRequestVerdict,
} from "@/modules/benchmarks/queries"
import { isOpen, type BenchmarkRun } from "@/modules/benchmarks/types"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"

/**
 * One ask and everything it produced.
 *
 * The page is split the way the object model is. The top card is the request:
 * declared, and frozen once made, because a request that changed its harness or
 * its criteria after its runs finished would rewrite what those runs claim. The
 * verdict, the bands and the runs below it are observed, and nothing there
 * takes an edit.
 */
export function BenchmarkRequestPage() {
  const { requestId } = useParams({
    from: "/_app/workbench/requests/$requestId",
  })
  const mock = isMockId(requestId)

  const query = useBenchmarkRequest(mock ? "" : requestId)
  const request = mock ? mockRequest(requestId) : query.data
  const isLoading = mock ? false : query.isLoading
  const error = mock ? null : query.error

  const open = !!request && isOpen(request.phase)
  const liveRuns = useRequestRuns(mock ? "" : requestId)
  const runs = mock
    ? {
        ...liveRuns,
        items: [mockRun(mockRunId(requestId.split("-")[2] ?? "conformance"))],
      }
    : liveRuns
  const { data: verdict } = useRequestVerdict(mock ? undefined : requestId)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Benchmark request" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load benchmark request")}
        </div>
      </div>
    )
  }

  const stepped = request ? STEPPED_MODES[request.mode] : undefined

  return (
    <div className="flex flex-col">
      <PageHeader
        title={
          request ? `${request.harness} ${request.mode}` : "Benchmark request"
        }
        description={requestId}
        readActions={
          <Button asChild variant="outline">
            <Link to="/workbench">
              <IconArrowLeft className="size-4" />
              All benchmarks
            </Link>
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}
        {mock && (
          <MockNotice what="This whole request is invented, reached from the list's Mock switch." />
        )}

        {request && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <LevelBadge level={request.level} />
              <StatusLabel
                tone={benchmarkPhaseTone(request.phase)}
                pulse={request.phase === "running"}
              >
                {request.phase}
              </StatusLabel>
              <span className="font-mono text-2xs text-muted-foreground">
                {request.runs.length} run
                {request.runs.length === 1 ? "" : "s"} ·{" "}
                {request.criteria_version}
              </span>
            </div>

            <p className="text-xs text-muted-foreground">
              This request measures {LEVEL_CLAIM[request.level]}. Its numbers do
              not combine with a result taken at another level.
            </p>

            {!request.homogeneous && (
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs text-warning">
                The coverage behind this target was not homogeneous: two of the
                things measured ran different artifacts or serving
                configurations. An accuracy score over that belongs to neither
                of them, and is graded inconclusive, not averaged.
              </div>
            )}

            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">The ask</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Level">
                      <LevelBadge level={request.level} />
                    </Field>
                    <Field label="Target">
                      <TargetLink
                        level={request.level}
                        target={request.target_id}
                      />
                    </Field>
                    <Field label="Harness">{request.harness}</Field>
                    <Field label="Mode">
                      {request.mode}
                      {stepped && (
                        <span className="text-muted-foreground">
                          {" "}
                          over {stepped}
                        </span>
                      )}
                    </Field>
                    <Field label="Criteria version">
                      {request.criteria_version}
                    </Field>
                    <Field label="Service tier">
                      <Dash>{request.tier}</Dash>
                    </Field>
                    <Field label="Requested by">{request.requested_by}</Field>
                    <Field label="Created">
                      {formatAbsolute(request.created_at)}
                    </Field>
                    <Field label="Image" className="sm:col-span-2">
                      <Dash>{request.image}</Dash>
                    </Field>
                    <Field label="Reason" className="sm:col-span-2">
                      <Dash>{request.reason}</Dash>
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">
                    What was behind the target
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <p className="text-2xs text-muted-foreground">
                    Snapshotted when the request expanded. It is allowed to go
                    stale: a workload rescaled since does not change what was
                    measured.
                  </p>
                  {request.coverage.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      Nothing was recorded behind this target: an endpoint-level
                      request is routed by weight, and attributing it to one
                      upstream would be a confident wrong answer.
                    </p>
                  ) : (
                    <div className="space-y-2">
                      {request.coverage.map((entry, index) => (
                        <div
                          key={`${entry.deployment_id ?? index}`}
                          className="flex flex-wrap items-center gap-1.5"
                        >
                          {entry.deployment_id && (
                            <Badge
                              variant="secondary"
                              className="font-mono text-2xs"
                              asChild
                            >
                              <Link
                                to="/deployments/$deploymentId"
                                params={{ deploymentId: entry.deployment_id }}
                              >
                                {shortenId(entry.deployment_id)}
                              </Link>
                            </Badge>
                          )}
                          {entry.cluster_id && (
                            <Badge
                              variant="outline"
                              className="font-mono text-2xs"
                            >
                              {shortenId(entry.cluster_id)}
                            </Badge>
                          )}
                          {entry.serving_digest && (
                            <Badge
                              variant="outline"
                              className="font-mono text-2xs"
                            >
                              {shortenId(entry.serving_digest)}
                            </Badge>
                          )}
                          {entry.artifact_id && (
                            <Badge
                              variant="outline"
                              className="font-mono text-2xs"
                            >
                              {shortenId(entry.artifact_id)}
                            </Badge>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>

            {verdict && <VerdictPanel verdict={verdict} title="Verdict" />}

            <SuiteBody
              harness={request.harness}
              requestId={requestId}
              runs={runs.items}
              open={open}
              stepped={!!stepped}
              parameters={request.parameters}
            />

            <div>
              <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
                Runs
              </h2>
              <DataTable
                columns={benchmarkColumns({
                  level: request.level,
                  showStep: true,
                  showDeployment: true,
                })}
                list={runs}
                rowKey={(run) => run.id}
                empty="This request has not expanded into any run."
              />
            </div>
          </>
        )}
      </div>
    </div>
  )
}

/**
 * The half of a suite that depends on what it was asking.
 *
 * Above this everything is the same for every kind, the ask, the coverage, the
 * verdict, the runs, because those are facts about a request and not about the
 * question it carried. Below it nothing is shared: a load curve, a claim
 * ledger, a transcript and a score table have no columns in common, and a
 * generic "results" panel that tried to hold all four would show none of them
 * properly.
 */
function SuiteBody({
  harness,
  requestId,
  runs,
  open,
  stepped,
  parameters,
}: {
  harness: string
  requestId: string
  runs: BenchmarkRun[] | undefined
  open: boolean
  stepped: boolean
  parameters: Record<string, unknown>
}) {
  switch (runKind(harness)) {
    case "performance":
      return (
        <PerformanceSuite
          requestId={requestId}
          runs={runs}
          open={open}
          stepped={stepped}
        />
      )
    case "conformance":
      return (
        <ConformanceSuite
          id={requestId}
          reference={
            typeof parameters.reference_base_url === "string"
              ? parameters.reference_base_url
              : null
          }
        />
      )
    case "agent":
      return <AgentSuite id={requestId} />
    case "evaluation":
      return <EvaluationSuite id={requestId} />
    default:
      return null
  }
}
