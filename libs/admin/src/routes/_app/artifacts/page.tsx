import { DataTable, type Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { formatBytes, formatParameters } from "@/modules/demand/display"
import { useArtifacts } from "@/modules/demand/queries"
import type { Artifact } from "@/modules/demand/types"

const columns: Column<Artifact>[] = [
  {
    key: "name",
    header: "Artifact",
    cell: (artifact) => (
      <span className="block">
        <span className="font-mono text-xs">{artifact.name}</span>
        <Id value={artifact.id} className="ml-2" />
      </span>
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
    key: "warm",
    header: "",
    align: "right",
    cell: (artifact) => (
      <NewOperationDialog
        defaultType="cache.warm"
        defaultTarget={artifact.id}
        trigger={
          <Button size="sm" variant="ghost" className="font-mono text-2xs">
            warm
          </Button>
        }
      />
    ),
  },
]

/** Immutable and content-addressed: identity is the digest, not the name. */
export function ArtifactsPage() {
  const list = useArtifacts()
  return (
    <div className="flex flex-col">
      <PageHeader
        title="Artifacts"
        description="declared · immutable, content-addressed bytes"
      />
      <div className="space-y-4 p-4">
        <DataTable
          columns={columns}
          list={list}
          rowKey={(artifact) => artifact.id}
          empty="No artifacts registered."
        />
      </div>
    </div>
  )
}
