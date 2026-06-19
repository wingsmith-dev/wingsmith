import { DataTable, type Column } from "@/components/layout/data-table"
import { DeleteDialog } from "@/components/layout/destructive"
import { Field, FieldGrid } from "@/components/layout/field"
import { CopyableId, Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import {
  StatusDot,
  StatusLabel,
  type StatusTone,
} from "@/components/layout/status-dot"
import { ArtifactFitmentPanel } from "@/components/demand/artifact-fitment"
import { RefreshSourceButton } from "@/components/demand/artifact-refresh"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import {
  acceptanceFloor,
  formatBytes,
  formatParameters,
  formatScore,
} from "@/modules/demand/display"
import { footprintOfServing, footprintSummary } from "@/modules/demand/deploy"
import {
  useArtifact,
  useArtifactBaselines,
  useArtifactUsage,
  useDeleteArtifact,
  useDerivedArtifacts,
  useWorkloads,
} from "@/modules/demand/queries"
import { useEvents } from "@/modules/machinery/queries"
import { severityTone } from "@/modules/machinery/display"
import type {
  Artifact,
  ArtifactBaseline,
  ArtifactUpstream,
  Workload,
} from "@/modules/demand/types"
import { ArtifactForm } from "../ArtifactForm"
import { IconArrowLeft, IconExternalLink } from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"

function Json({ value }: { value: unknown }) {
  return (
    <pre className="overflow-x-auto rounded-md border bg-muted/30 p-3 font-mono text-2xs">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

/** A headline number for the stat strip. Not a link — this is a detail page. */
function StatCard({
  label,
  value,
  tone,
}: {
  label: string
  value: React.ReactNode
  tone?: "ok" | "warn"
}) {
  return (
    <Card size="sm" className="gap-1.5 px-3">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </span>
      <span
        className={
          "font-heading text-xl leading-none font-semibold tabular-nums " +
          (tone === "ok"
            ? "text-emerald-500"
            : tone === "warn"
              ? "text-amber-500"
              : "")
        }
      >
        {value}
      </span>
    </Card>
  )
}

/** The digest is the identity, so it is shown whole here — the list truncates it. */
export function ArtifactDetailPage() {
  const { artifactId } = useParams({ from: "/_app/artifacts/$artifactId" })
  const { data: artifact, isLoading, error } = useArtifact(artifactId)
  const workloads = useWorkloads({ artifact: artifactId })
  const baselines = useArtifactBaselines(artifactId)
  const usage = useArtifactUsage(artifactId)
  const derived = useDerivedArtifacts(artifactId)
  const remove = useDeleteArtifact()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Artifact" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load artifact")}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={artifact?.name ?? "Artifact"}
        description={artifact?.id}
        actions={
          artifact && (
            <div className="flex gap-2">
              {/* The primary action on an artifact: the one thing you registered it to do. */}
              <Button asChild>
                <Link to="/workloads/new" search={{ artifact: artifact.id }}>
                  Deploy
                </Link>
              </Button>
              <Button variant="outline" onClick={() => setEditing(true)}>
                Edit
              </Button>
              <Button variant="outline" onClick={() => setDoomed(true)}>
                Delete
              </Button>
              <RefreshSourceButton artifact={artifact} />
              <NewOperationDialog
                defaultType="cache.warm"
                defaultTarget={artifact.id}
                trigger={<Button variant="outline">Warm</Button>}
              />
            </div>
          )
        }
        readActions={
          <Button asChild variant="outline">
            <Link to="/artifacts">
              <IconArrowLeft className="size-4" />
              All artifacts
            </Link>
          </Button>
        }
      />

      {artifact && editing && (
        <ArtifactForm
          key={artifact.revision}
          open
          artifact={artifact}
          onOpenChange={(open) => !open && setEditing(false)}
        />
      )}
      {artifact && (
        <DeleteDialog
          icon="artifact"
          open={doomed}
          onOpenChange={setDoomed}
          kind="artifact"
          name={artifact.name}
          note="Workloads that serve it will refuse to resolve."
          error={remove.error}
          pending={remove.isPending}
          onConfirm={() =>
            remove.mutate(artifact.id, {
              onSuccess: () => {
                setDoomed(false)
                toast.success("Artifact deleted", {
                  description: artifact.name,
                })
                navigate({ to: "/artifacts" })
              },
              onError: (e) =>
                toast.error(formatError(e, "Could not delete artifact")),
            })
          }
        />
      )}

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-56 w-full" />}

        {artifact && (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
              <StatCard label="Size" value={formatBytes(artifact.size_bytes)} />
              {/* TODO-API Artifact carries no file manifest — showing registered locations as the file count. */}
              <StatCard label="Files" value={artifact.locations.length} />
              <StatCard
                label="Workloads"
                value={usage.data?.workload_count ?? workloads.loaded}
              />
              <StatCard
                label="Verification"
                value={artifact.verified ? "Verified" : "Unverified"}
                tone={artifact.verified ? "ok" : "warn"}
              />
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              <div className="flex flex-col gap-4 lg:col-span-2">
                <ArtifactFitmentPanel
                  artifact={artifact}
                  onPick={(footprint) =>
                    navigate({
                      to: "/workloads/new",
                      search: {
                        artifact: artifact.id,
                        class: footprint.device_class,
                        devices: footprint.device_count,
                        tp: footprint.tensor_parallel,
                        pp: footprint.pipeline_parallel,
                        ctx: footprint.context_length,
                        quant: footprint.quantization || undefined,
                      },
                    })
                  }
                />
                <FilesPanel artifact={artifact} />
                <ServedByPanel artifactId={artifactId} workloads={workloads} />
                <BaselinesPanel baselines={baselines} />
                <LineagePanel artifact={artifact} derived={derived} />
              </div>

              <div className="flex flex-col gap-4">
                <DetailsPanel artifact={artifact} />
                <FingerprintPanel artifact={artifact} />
                <ActivityPanel artifactId={artifactId} />
                <MetadataPanel artifact={artifact} />
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

/** The content-addressed manifest. Rendered from registered locations. */
function FilesPanel({ artifact }: { artifact: Artifact }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">
          Files &amp; locations ({artifact.locations.length})
        </CardTitle>
        <p className="font-mono text-2xs text-muted-foreground/60">
          Content-addressed manifest
        </p>
      </CardHeader>
      <CardContent>
        {artifact.locations.length === 0 ? (
          <p className="font-mono text-2xs text-muted-foreground/50">
            No location registered — nothing can pull these bytes yet.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
                  <th className="p-2 text-left font-medium">URI</th>
                  <th className="p-2 text-left font-medium">Scheme</th>
                  <th className="p-2 text-left font-medium">Region</th>
                  <th className="p-2 text-left font-medium">Preferred</th>
                </tr>
              </thead>
              <tbody>
                {artifact.locations.map((location) => (
                  <tr key={location.uri} className="border-b last:border-0">
                    <td className="p-2 font-mono break-all">{location.uri}</td>
                    <td className="p-2 font-mono text-muted-foreground">
                      {location.scheme}
                    </td>
                    <td className="p-2 font-mono">
                      <Dash>{location.region}</Dash>
                    </td>
                    <td className="p-2">
                      {location.preferred ? (
                        <Badge
                          variant="secondary"
                          className="font-mono text-2xs"
                        >
                          preferred
                        </Badge>
                      ) : (
                        <Dash>{null}</Dash>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/** The workloads serving these weights, which is what a recipe list used to be. */
function ServedByPanel({
  artifactId,
  workloads,
}: {
  artifactId: string
  workloads: ReturnType<typeof useWorkloads>
}) {
  const columns: Column<Workload>[] = [
    {
      key: "name",
      header: "Workload",
      cell: (workload) => (
        <Link
          to="/workloads/$workloadId"
          params={{ workloadId: workload.id }}
          className="inline-flex items-center gap-1.5 font-mono text-xs hover:text-primary"
        >
          {workload.name}
          <IconExternalLink className="size-3 text-muted-foreground" />
        </Link>
      ),
    },
    {
      key: "serving",
      header: "Serving",
      cell: (workload) => (
        <span className="font-mono text-2xs">
          {footprintSummary(footprintOfServing(workload.serving))}
        </span>
      ),
    },
    {
      key: "engine",
      header: "Engine",
      secondary: true,
      cell: (workload) => (
        <span className="font-mono text-2xs">
          {workload.serving.engine} · runtime{" "}
          {workload.serving.engine_runtime_version}
        </span>
      ),
    },
    {
      key: "phase",
      header: "Phase",
      secondary: true,
      cell: (workload) => (
        <span className="font-mono text-2xs">{workload.phase}</span>
      ),
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      cell: (workload) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(workload.updated_at ?? workload.created_at)}
        </span>
      ),
    },
  ]

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm">Served by</CardTitle>
        <Button
          asChild
          size="sm"
          variant="ghost"
          className="font-mono text-2xs"
        >
          <Link to="/workloads" search={{ artifact: artifactId }}>
            View all
          </Link>
        </Button>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          list={workloads}
          rowKey={(workload) => workload.id}
          empty="No workload serves this artifact."
        />
      </CardContent>
    </Card>
  )
}

/**
 * Declared quality floors, and the acceptance floor a verdict grades against.
 * These feed benchmark verdicts, so the header links out to the runs.
 */
function BaselinesPanel({
  baselines,
}: {
  baselines: ReturnType<typeof useArtifactBaselines>
}) {
  const rows = baselines.data ?? []
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-2">
        <div>
          <CardTitle className="text-sm">Quality baselines</CardTitle>
          <p className="font-mono text-2xs text-muted-foreground/60">
            Declared floors graded against benchmark runs
          </p>
        </div>
        <Button
          asChild
          size="sm"
          variant="ghost"
          className="font-mono text-2xs"
        >
          <Link to="/workbench">Workbench</Link>
        </Button>
      </CardHeader>
      <CardContent>
        {baselines.isLoading ? (
          <Skeleton className="h-16 w-full" />
        ) : rows.length === 0 ? (
          <p className="font-mono text-2xs text-muted-foreground/50">
            No baseline recorded — an acceptance review has nothing to compare
            against.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
                  <th className="p-2 text-left font-medium">Dataset</th>
                  <th className="p-2 text-left font-medium">Metric</th>
                  <th className="p-2 text-right font-medium">Value</th>
                  <th className="p-2 text-right font-medium">Floor</th>
                  <th className="p-2 text-left font-medium">Recorded by</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((baseline: ArtifactBaseline) => (
                  <tr key={baseline.id} className="border-b last:border-0">
                    <td className="p-2 font-mono">{baseline.dataset}</td>
                    <td className="p-2 font-mono">{baseline.metric}</td>
                    <td className="p-2 text-right font-mono tabular-nums">
                      {formatScore(baseline.value)}
                    </td>
                    <td className="p-2 text-right font-mono text-muted-foreground tabular-nums">
                      {formatScore(acceptanceFloor(baseline.value))}
                    </td>
                    <td className="p-2 font-mono text-muted-foreground">
                      {baseline.recorded_by}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/** Where these bytes came from, and what they were derived from. */
function LineagePanel({
  artifact,
  derived,
}: {
  artifact: Artifact
  derived: ReturnType<typeof useDerivedArtifacts>
}) {
  const upstream = artifact.upstream ?? ({} as ArtifactUpstream)
  const children = derived.items ?? []
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Lineage</CardTitle>
      </CardHeader>
      <CardContent>
        <FieldGrid>
          <Field label="Derived from">
            {artifact.base_artifact_id ? (
              <Link
                to="/artifacts/$artifactId"
                params={{ artifactId: artifact.base_artifact_id }}
                className="inline-flex items-center gap-1.5 hover:text-primary"
              >
                {artifact.base_artifact_id}
                <IconExternalLink className="size-3 text-muted-foreground" />
              </Link>
            ) : (
              <Dash>{null}</Dash>
            )}
          </Field>
          <Field label="Provider">
            <Dash>{upstream.provider}</Dash>
          </Field>
          <Field label="Provider model">
            <Dash>{upstream.provider_model_id}</Dash>
          </Field>
          <Field label="Source repo" className="sm:col-span-2">
            <span className="break-all">
              <Dash>{upstream.source_uri}</Dash>
            </span>
          </Field>
          <Field label="Recorded by">
            <Dash>{upstream.recorded_by}</Dash>
          </Field>
          <Field label="Recorded at">
            {upstream.recorded_at ? (
              formatAbsolute(upstream.recorded_at)
            ) : (
              <Dash>{null}</Dash>
            )}
          </Field>
          <Field label="Downstream artifacts" className="sm:col-span-2">
            {children.length === 0 ? (
              <Dash>{null}</Dash>
            ) : (
              <div className="flex flex-col gap-1">
                {children.map((child) => (
                  <Link
                    key={child.id}
                    to="/artifacts/$artifactId"
                    params={{ artifactId: child.id }}
                    className="inline-flex items-center gap-1.5 font-mono text-xs hover:text-primary"
                  >
                    {child.name}
                    <IconExternalLink className="size-3 text-muted-foreground" />
                  </Link>
                ))}
              </div>
            )}
          </Field>
        </FieldGrid>
      </CardContent>
    </Card>
  )
}

/** The identity block — what the publisher said and what we believe. */
function DetailsPanel({ artifact }: { artifact: Artifact }) {
  const upstream: ArtifactUpstream = {
    input_modalities: [],
    structured_output_modes: [],
    ...(artifact.upstream as Partial<ArtifactUpstream> | undefined),
  } as ArtifactUpstream
  const tone: StatusTone = artifact.verified ? "ok" : "warn"
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Details</CardTitle>
      </CardHeader>
      <CardContent>
        <FieldGrid>
          <Field label="Status">
            <StatusLabel tone={tone}>
              {artifact.verified ? "Ready" : "Unverified"}
            </StatusLabel>
          </Field>
          <Field label="Kind">{artifact.kind}</Field>
          <Field label="Format">
            <Dash>{artifact.format}</Dash>
          </Field>
          <Field label="Parameters">
            {formatParameters(artifact.parameter_count)}
          </Field>
          <Field label="Context length">
            {upstream.context_length != null ? (
              upstream.context_length.toLocaleString()
            ) : (
              <Dash>{null}</Dash>
            )}
          </Field>
          <Field label="Max output tokens">
            {upstream.max_output_tokens != null ? (
              upstream.max_output_tokens.toLocaleString()
            ) : (
              <Dash>{null}</Dash>
            )}
          </Field>
          <Field label="Input modalities" className="sm:col-span-2">
            {upstream.input_modalities.length > 0 ? (
              <div className="flex flex-wrap gap-1">
                {upstream.input_modalities.map((m) => (
                  <Badge
                    key={m}
                    variant="outline"
                    className="font-mono text-2xs"
                  >
                    {m}
                  </Badge>
                ))}
              </div>
            ) : (
              <Dash>{null}</Dash>
            )}
          </Field>
          <Field label="Licence">
            <Dash>{artifact.licence}</Dash>
          </Field>
          <Field label="Upstream licence">
            <Dash>{upstream.licence}</Dash>
          </Field>
          <Field label="Project">
            <Dash>{artifact.project_id}</Dash>
          </Field>
          <Field label="Revision">{artifact.revision}</Field>
          <Field label="Created">{formatRelative(artifact.created_at)}</Field>
          <Field label="Updated">
            {artifact.updated_at ? (
              formatRelative(artifact.updated_at)
            ) : (
              <Dash>{null}</Dash>
            )}
          </Field>
        </FieldGrid>
      </CardContent>
    </Card>
  )
}

/** The digest, and whether the bytes behind it have been verified. */
function FingerprintPanel({ artifact }: { artifact: Artifact }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">
          Fingerprint &amp; verification
        </CardTitle>
      </CardHeader>
      <CardContent>
        <FieldGrid>
          <Field label="Fingerprint" className="sm:col-span-2">
            <CopyableId
              value={artifact.digest}
              className="text-2xs break-all text-muted-foreground/80"
            />
          </Field>
          <Field label="Id">
            <Id value={artifact.id} />
          </Field>
          <Field label="Signature">
            {artifact.verified ? (
              <Badge
                variant="secondary"
                className="border-emerald-500/30 bg-emerald-500/10 font-mono text-2xs text-emerald-500"
              >
                verified
              </Badge>
            ) : (
              <Badge
                variant="secondary"
                className="border-amber-500/30 bg-amber-500/10 font-mono text-2xs text-amber-500"
              >
                unverified
              </Badge>
            )}
          </Field>
          {/* TODO-API No re-verification timestamp on the object. */}
          <Field label="Last verified">
            <Dash>{null}</Dash>
          </Field>
          {/* TODO-API No security-scan result on the object. */}
          <Field label="Security scan">
            <Dash>{null}</Dash>
          </Field>
        </FieldGrid>
      </CardContent>
    </Card>
  )
}

/** Everything the plane has recorded against this artifact, newest first. */
function ActivityPanel({ artifactId }: { artifactId: string }) {
  const events = useEvents({ subject: artifactId })
  const rows = events.items ?? []
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm">Activity</CardTitle>
        <Button
          asChild
          size="sm"
          variant="ghost"
          className="font-mono text-2xs"
        >
          <Link to="/events" search={{ subject: artifactId }}>
            All events
          </Link>
        </Button>
      </CardHeader>
      <CardContent>
        {events.isLoading ? (
          <Skeleton className="h-16 w-full" />
        ) : rows.length === 0 ? (
          <p className="font-mono text-2xs text-muted-foreground/50">
            Nothing recorded against this artifact yet.
          </p>
        ) : (
          <ol className="space-y-3">
            {rows.map((event) => (
              <li key={event.id} className="flex gap-3">
                <StatusDot
                  tone={severityTone(event.severity)}
                  className="mt-1 shrink-0"
                />
                <div className="min-w-0 space-y-0.5">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-mono text-2xs">{event.kind}</span>
                    <span
                      className="font-mono text-2xs text-muted-foreground/50"
                      title={formatAbsolute(event.occurred_at)}
                    >
                      {formatRelative(event.occurred_at)}
                    </span>
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {event.message}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  )
}

/** The free-form bag. Rendered raw — it is not a modelled surface. */
function MetadataPanel({ artifact }: { artifact: Artifact }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Metadata</CardTitle>
      </CardHeader>
      <CardContent>
        {Object.keys(artifact.metadata).length === 0 ? (
          <p className="font-mono text-2xs text-muted-foreground/50">none</p>
        ) : (
          <Json value={artifact.metadata} />
        )}
      </CardContent>
    </Card>
  )
}
