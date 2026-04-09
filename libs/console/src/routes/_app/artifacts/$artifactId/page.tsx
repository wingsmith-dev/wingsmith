import { Field, FieldGrid } from "@/components/layout/field"
import { CopyableId, Dash } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { DeleteDialog } from "@/components/layout/destructive"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { formatBytes, formatParameters } from "@/modules/demand/display"
import { useArtifact, useDeleteArtifact } from "@/modules/demand/queries"
import { ArtifactForm } from "../ArtifactForm"
import { IconArrowLeft } from "@tabler/icons-react"
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

/** The digest is the identity, so it is shown whole here — the list truncates it. */
export function ArtifactDetailPage() {
  const { artifactId } = useParams({ from: "/_app/artifacts/$artifactId" })
  const { data: artifact, isLoading, error } = useArtifact(artifactId)
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
              <Button
                size="sm"
                variant="outline"
                onClick={() => setEditing(true)}
              >
                Edit
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setDoomed(true)}
              >
                Delete
              </Button>
              <NewOperationDialog
                defaultType="cache.warm"
                defaultTarget={artifact.id}
                trigger={
                  <Button size="sm" variant="outline">
                    Warm
                  </Button>
                }
              />
            </div>
          )
        }
        readActions={
          <Button asChild variant="outline" size="sm">
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
          note="Recipes that reference it will refuse to resolve."
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
            <div className="flex flex-wrap items-center gap-3">
              <Badge variant="secondary" className="font-mono text-2xs">
                {artifact.kind}
              </Badge>
              <span className="font-mono text-2xs text-muted-foreground/60">
                {formatBytes(artifact.size_bytes)} ·{" "}
                {formatParameters(artifact.parameter_count)} params
              </span>
              <Button
                asChild
                size="sm"
                variant="ghost"
                className="font-mono text-2xs"
              >
                <Link to="/recipes" search={{ artifact: artifact.id }}>
                  Recipes using this artifact
                </Link>
              </Button>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Identity</CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Name">{artifact.name}</Field>
                    <Field label="Id">
                      <CopyableId
                        value={artifact.id}
                        className="text-2xs text-muted-foreground/60"
                      />
                    </Field>
                    <Field label="Digest" className="sm:col-span-2">
                      <span className="break-all">{artifact.digest}</span>
                    </Field>
                    <Field label="Kind">{artifact.kind}</Field>
                    <Field label="Format">
                      <Dash>{artifact.format}</Dash>
                    </Field>
                    <Field label="Base artifact" className="sm:col-span-2">
                      {artifact.base_artifact_id ? (
                        <Link
                          to="/artifacts/$artifactId"
                          params={{ artifactId: artifact.base_artifact_id }}
                          className="hover:underline"
                        >
                          {artifact.base_artifact_id}
                        </Link>
                      ) : (
                        <Dash>{null}</Dash>
                      )}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">
                    Bytes and provenance
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <FieldGrid>
                    <Field label="Size">
                      {formatBytes(artifact.size_bytes)}{" "}
                      <span className="text-muted-foreground/50">
                        ({artifact.size_bytes.toLocaleString()} B)
                      </span>
                    </Field>
                    <Field label="Parameters">
                      {formatParameters(artifact.parameter_count)}
                    </Field>
                    <Field label="Licence">
                      <Dash>{artifact.licence}</Dash>
                    </Field>
                    <Field label="Project">
                      <Dash>{artifact.project_id}</Dash>
                    </Field>
                    <Field label="Revision">{artifact.revision}</Field>
                    <Field label="Created">
                      {formatRelative(artifact.created_at)}
                    </Field>
                    <Field label="Created at">
                      {formatAbsolute(artifact.created_at)}
                    </Field>
                    <Field label="Updated at">
                      {formatAbsolute(artifact.updated_at)}
                    </Field>
                  </FieldGrid>
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">
                  Locations ({artifact.locations.length})
                </CardTitle>
              </CardHeader>
              <CardContent>
                {artifact.locations.length === 0 ? (
                  <p className="font-mono text-2xs text-muted-foreground/50">
                    No location registered — nothing can pull these bytes yet.
                  </p>
                ) : (
                  <Json value={artifact.locations} />
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Metadata</CardTitle>
              </CardHeader>
              <CardContent>
                {Object.keys(artifact.metadata).length === 0 ? (
                  <p className="font-mono text-2xs text-muted-foreground/50">
                    none
                  </p>
                ) : (
                  <Json value={artifact.metadata} />
                )}
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </div>
  )
}
