import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatBytes, formatParameters } from "@/modules/demand/display"
import { useArtifacts, useDeleteArtifact } from "@/modules/demand/queries"
import type { Artifact } from "@/modules/demand/types"
import { formatError } from "@/lib/format"
import { ArtifactForm } from "./ArtifactForm"
import { useState } from "react"
import { toast } from "sonner"

function columns(
  onEdit: (artifact: Artifact) => void,
  onDelete: (artifact: Artifact) => void
): Column<Artifact>[] {
  return [
  {
    key: "name",
    header: "Artifact",
    cell: (artifact) => (
      <ResourceCell kind="artifact">
        <span className="block">
          <span className="font-mono text-xs">{artifact.name}</span>
          <Id value={artifact.id} className="ml-2" />
        </span>
      </ResourceCell>
    ),
  },
  {
    key: "digest",
    header: "Digest",
    cell: (artifact) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {artifact.digest.slice(0, 23)}…
      </span>
    ),
  },
  {
    key: "kind",
    header: "Kind",
    cell: (artifact) => (
      <Badge variant="secondary" className="font-mono text-2xs">
        {artifact.kind}
      </Badge>
    ),
  },
  {
    key: "format",
    header: "Format",
    cell: (artifact) => (
      <span className="font-mono text-2xs text-muted-foreground">
        <Dash>{artifact.format}</Dash>
      </span>
    ),
  },
  {
    key: "size",
    header: "Size",
    align: "right",
    cell: (artifact) => (
      <span className="font-mono text-2xs">{formatBytes(artifact.size_bytes)}</span>
    ),
  },
  {
    key: "params",
    header: "Parameters",
    align: "right",
    cell: (artifact) => (
      <span className="font-mono text-2xs">
        {formatParameters(artifact.parameter_count)}
      </span>
    ),
  },
  {
    key: "locations",
    header: "Locations",
    secondary: true,
    cell: (artifact) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {artifact.locations.length === 0
          ? "none"
          : artifact.locations
              .map((l) => `${l.scheme}${l.preferred ? "*" : ""}`)
              .join(" ")}
      </span>
    ),
  },
  {
    key: "licence",
    header: "Licence",
    secondary: true,
    cell: (artifact) => (
      <span className="font-mono text-2xs text-muted-foreground">
        <Dash>{artifact.licence}</Dash>
      </span>
    ),
  },
  {
    key: "actions",
    header: "",
    align: "right",
    cell: (artifact) => (
      <div className="flex items-center justify-end gap-1">
        <NewOperationDialog
          defaultType="cache.warm"
          defaultTarget={artifact.id}
          trigger={
            <Button size="sm" variant="ghost" className="font-mono text-2xs">
              warm
            </Button>
          }
        />
        <RowActions
          actions={[
            { label: "Edit", onSelect: () => onEdit(artifact) },
            {
              label: "Delete",
              destructive: true,
              onSelect: () => onDelete(artifact),
            },
          ]}
        />
      </div>
    ),
  },
  ]
}

/** Immutable and content-addressed: identity is the digest, not the name. */
export function ArtifactsPage() {
  const list = useArtifacts()
  const remove = useDeleteArtifact()
  const [editing, setEditing] = useState<Artifact | undefined>()
  const [creating, setCreating] = useState(false)
  const [doomed, setDoomed] = useState<Artifact | undefined>()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Artifacts"
        description="declared · immutable, content-addressed bytes"
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            New artifact
          </Button>
        }
      />
      <div className="space-y-4 p-4">
        <DataTable
          columns={columns(setEditing, setDoomed)}
          list={list}
          rowKey={(artifact) => artifact.id}
          empty="No artifacts registered."
        />
      </div>

      {creating && (
        <ArtifactForm open onOpenChange={(open) => !open && setCreating(false)} />
      )}
      {editing && (
        <ArtifactForm
          key={editing.revision}
          open
          artifact={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="artifact"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="artifact"
        name={doomed?.name ?? ""}
        note="Recipes that reference it will refuse to resolve."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Artifact deleted", { description: doomed.name })
            },
            onError: (e) => toast.error(formatError(e, "Could not delete artifact")),
          })
        }
      />
    </div>
  )
}
