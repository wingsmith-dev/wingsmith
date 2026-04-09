import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { BenchmarkSummaryView } from "@/components/benchmarks/summary-view"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { benchmarkPhaseTone } from "@/modules/benchmarks/display"
import { useBenchmark } from "@/modules/benchmarks/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"

export function BenchmarkDetailPage() {
  const { benchmarkId } = useParams({ from: "/_app/benchmarks/$benchmarkId" })
  const { data: run, isLoading, error } = useBenchmark(benchmarkId)

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
        actions={
          <div className="flex gap-2">
            {run?.operation_id && (
              <Button asChild variant="outline" size="sm">
                <Link
                  to="/operations/$operationId"
                  params={{ operationId: run.operation_id }}
                >
                  Operation
                </Link>
              </Button>
            )}
            {run && (
              <Button asChild variant="outline" size="sm">
                <Link
                  to="/deployments/$deploymentId"
                  params={{ deploymentId: run.deployment_id }}
                >
                  Deployment
                </Link>
              </Button>
            )}
            <Button asChild variant="outline" size="sm">
              <Link to="/benchmarks">
                <IconArrowLeft className="size-4" />
                All benchmarks
              </Link>
            </Button>
          </div>
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {run && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <StatusLabel
                tone={benchmarkPhaseTone(run.phase)}
                pulse={run.phase === "running"}
              >
                {run.phase}
              </StatusLabel>
              <span className="font-mono text-2xs text-muted-foreground/60">
                {run.harness}
                {run.harness_version ? ` ${run.harness_version}` : ""}
              </span>
            </div>

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
                    <Field label="Requested by">{run.requested_by}</Field>
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
                      {run.provenance.model}
                    </Field>
                    <Field label="Deployment">
                      <Link
                        to="/deployments/$deploymentId"
                        params={{ deploymentId: run.deployment_id }}
                        className="hover:underline"
                      >
                        {run.deployment_id}
                      </Link>
                    </Field>
                    <Field label="Generation">
                      {run.provenance.deployment_generation}
                    </Field>
                    <Field label="Recipe">
                      <Dash>{run.provenance.recipe_id}</Dash>
                    </Field>
                    <Field label="Recipe version">
                      <Dash>
                        {run.provenance.recipe_version === null
                          ? null
                          : String(run.provenance.recipe_version)}
                      </Dash>
                    </Field>
                  </FieldGrid>
                  {Object.keys(run.provenance.engine_args).length > 0 && (
                    <pre className="overflow-x-auto rounded-lg border bg-muted/30 p-2 font-mono text-2xs">
                      {JSON.stringify(run.provenance.engine_args, null, 2)}
                    </pre>
                  )}
                </CardContent>
              </Card>
            </div>

            <div>
              <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
                Result
              </h2>
              {run.summary ? (
                <BenchmarkSummaryView summary={run.summary} />
              ) : (
                <div className="rounded-lg border bg-card p-6 text-center text-xs text-muted-foreground">
                  {run.phase === "pending" || run.phase === "running"
                    ? "The harness has not reported yet."
                    : "This run produced no result."}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
