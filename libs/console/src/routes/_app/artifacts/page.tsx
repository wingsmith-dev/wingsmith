import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { ResourceCell } from "@/components/layout/resource-cell"
import { ResourceIcon } from "@/components/layout/resource-icon"
import { StatusLabel, type StatusTone } from "@/components/layout/status-dot"
import { NewOperationDialog } from "@/components/machinery/new-operation"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatBytes } from "@/modules/demand/display"
import {
  useArtifactQuantizations,
  useArtifactUsages,
  useArtifacts,
  useDeleteArtifact,
} from "@/modules/demand/queries"
import {
  ARTIFACT_KINDS,
  type Artifact,
  type ArtifactUsage,
} from "@/modules/demand/types"
import { formatError, formatRelative, shortenId } from "@/lib/format"
import { ArtifactForm } from "./ArtifactForm"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { Link } from "@tanstack/react-router"

/**
 * The artifact lifecycle the reference draws — ready / importing / failed /
 * deprecated — is not a field the plane sends today. It is derived here from
 * `verified` and the location set so the filter and status column read the way
 * the design intends; the day the plane grows an explicit state, this collapses
 * to a field read.
 */
// TODO-API artifact has no lifecycle status; deriving from verified/locations.
type ArtifactStatus = "ready" | "importing" | "failed" | "deprecated"

const STATUS_TONES: Record<ArtifactStatus, StatusTone> = {
  ready: "ok",
  importing: "neutral",
  failed: "danger",
  deprecated: "warn",
}

const STATUS_TABS: { value: ArtifactStatus | typeof ALL; label: string }[] = [
  { value: ALL, label: "All" },
  { value: "ready", label: "Ready" },
  { value: "importing", label: "Importing" },
  { value: "failed", label: "Failed" },
  { value: "deprecated", label: "Deprecated" },
]

function artifactStatus(artifact: Artifact): ArtifactStatus {
  // TODO-API mocked lifecycle: no bytes located yet reads as importing.
  if (artifact.locations.length === 0) return "importing"
  if (!artifact.verified) return "failed"
  return "ready"
}

function columns(
  usages: Map<string, ArtifactUsage>,
  quantizations: Map<string, string[]>,
  onEdit: (artifact: Artifact) => void,
  onDelete: (artifact: Artifact) => void
): Column<Artifact>[] {
  return [
    {
      key: "name",
      header: "Artifact",
      cell: (artifact) => (
        <ResourceCell kind="artifact">
          <Link
            to="/artifacts/$artifactId"
            params={{ artifactId: artifact.id }}
            className="block"
          >
            <span className="font-mono text-xs">{artifact.name}</span>
            <Id value={shortenId(artifact.id)} className="ml-2" />
          </Link>
          <span className="block font-mono text-2xs text-muted-foreground">
            {artifact.upstream?.provider_model_id ?? artifact.kind}
          </span>
        </ResourceCell>
      ),
    },
    {
      key: "status",
      header: "Status",
      cell: (artifact) => {
        const status = artifactStatus(artifact)
        return (
          <StatusLabel
            tone={STATUS_TONES[status]}
            pulse={status === "importing"}
          >
            {status}
          </StatusLabel>
        )
      },
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
      key: "quantization",
      header: "Quantization",
      cell: (artifact) => {
        const quants = quantizations.get(artifact.id) ?? []
        return (
          <span className="font-mono text-2xs text-muted-foreground">
            <Dash>{quants.length > 0 ? quants.join(" · ") : null}</Dash>
          </span>
        )
      },
    },
    {
      key: "size",
      header: "Size",
      align: "right",
      cell: (artifact) => (
        <span className="font-mono text-2xs">
          {formatBytes(artifact.size_bytes)}
        </span>
      ),
    },
    {
      key: "fingerprint",
      header: "Fingerprint",
      cell: (artifact) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {artifact.digest.slice(0, 23)}…
        </span>
      ),
    },
    {
      key: "used_by",
      header: "Used by",
      align: "right",
      secondary: true,
      cell: (artifact) => {
        const n = usages.get(artifact.id)?.recipe_count ?? 0
        return (
          <span className="font-mono text-2xs text-muted-foreground">
            {n === 0 ? (
              <span className="text-muted-foreground/50">0 recipes</span>
            ) : (
              `${n} ${n === 1 ? "recipe" : "recipes"}`
            )}
          </span>
        )
      },
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      secondary: true,
      cell: (artifact) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(artifact.updated_at ?? artifact.created_at)}
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

interface TileProps {
  label: string
  value: React.ReactNode
  tone?: "warn"
}

function SummaryTile({ label, value, tone }: TileProps) {
  return (
    <Card size="sm" className="gap-2 px-3">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </span>
      <span
        className={
          tone === "warn"
            ? "font-heading text-2xl leading-none font-semibold tabular-nums text-amber-500"
            : "font-heading text-2xl leading-none font-semibold tabular-nums"
        }
      >
        {value}
      </span>
    </Card>
  )
}

/** Immutable and content-addressed: identity is the digest, not the name. */
export function ArtifactsPage() {
  const [kind, setKind] = useState<string>(ALL)
  const [status, setStatus] = useState<ArtifactStatus | typeof ALL>(ALL)
  const [term, setTerm] = useState("")
  const list = useArtifacts({
    kind: kind === ALL ? undefined : kind,
    search: term.trim() || undefined,
  })
  const remove = useDeleteArtifact()
  const [editing, setEditing] = useState<Artifact | undefined>()
  const [creating, setCreating] = useState(false)
  const [doomed, setDoomed] = useState<Artifact | undefined>()

  const ids = useMemo(
    () => (list.items ?? []).map((a) => a.id),
    [list.items]
  )
  const usages = useArtifactUsages(ids)
  const quantizations = useArtifactQuantizations(ids)

  const rows = useMemo(
    () =>
      (list.items ?? []).filter(
        (a) => status === ALL || artifactStatus(a) === status
      ),
    [list.items, status]
  )

  const summary = useMemo(() => {
    const items = list.items ?? []
    return {
      count: items.length,
      storage: items.reduce((sum, a) => sum + a.size_bytes, 0),
      inUse: items.filter((a) => {
        const usage = usages.get(a.id)
        return (
          !!usage &&
          usage.recipe_count + usage.workload_count + usage.downstream_count > 0
        )
      }).length,
      attention: items.filter((a) => artifactStatus(a) === "failed").length,
    }
  }, [list.items, usages])

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
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <SummaryTile label="Artifacts" value={summary.count} />
          <SummaryTile
            label="Total storage"
            value={formatBytes(summary.storage)}
          />
          <SummaryTile label="In use" value={summary.inUse} />
          <SummaryTile
            label="Needs attention"
            value={summary.attention}
            tone="warn"
          />
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2">
          <Tabs
            value={status}
            onValueChange={(v) => setStatus(v as ArtifactStatus | typeof ALL)}
          >
            <TabsList>
              {STATUS_TABS.map((tab) => (
                <TabsTrigger key={tab.value} value={tab.value}>
                  {tab.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <ResourceIcon
                kind="artifact"
                className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
              />
              <input
                type="search"
                value={term}
                onChange={(e) => setTerm(e.target.value)}
                placeholder="Search artifacts…"
                className="h-8 w-56 rounded-md border border-input bg-card pl-8 pr-3 font-mono text-xs placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <FilterSelect
              label="Kind"
              value={kind}
              onChange={setKind}
              options={ARTIFACT_KINDS.map((k) => ({ value: k, label: k }))}
            />
          </div>
        </div>

        <DataTable
          columns={columns(usages, quantizations, setEditing, setDoomed)}
          list={{ ...list, items: rows }}
          rowKey={(artifact) => artifact.id}
          empty="No artifacts registered."
        />
      </div>

      {creating && (
        <ArtifactForm
          open
          onOpenChange={(open) => !open && setCreating(false)}
        />
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
            onError: (e) =>
              toast.error(formatError(e, "Could not delete artifact")),
          })
        }
      />
    </div>
  )
}
