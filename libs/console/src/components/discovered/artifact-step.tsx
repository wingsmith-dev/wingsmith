import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dash } from "@/components/layout/mono"
import { formatError } from "@/lib/format"
import { useCreateArtifact } from "@/modules/demand/queries"
import type { Artifact } from "@/modules/demand/types"
import type { ArtifactDraft } from "@/modules/discovered/types"
import { toast } from "sonner"

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <span className="font-mono text-2xs text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right font-mono text-xs break-all">
        {value}
      </span>
    </div>
  )
}

/**
 * The first step of adoption: which weights this workload is serving.
 *
 * A serving block must point at an artifact, and a workload Wingsmith never declared
 * has none, so adoption would otherwise dead-end on a form asking for an
 * artifact nobody had created. The step exists to create it from
 * the same reading, and to say plainly what that reading is worth: the digest is
 * `--model` as someone typed it, not a hash of any bytes, so the artifact is
 * written unverified and stays that way until something resolves the weights.
 */
export function ArtifactStep({
  draft,
  onResolved,
}: {
  draft: ArtifactDraft
  onResolved: (artifactId: string) => void
}) {
  const create = useCreateArtifact()

  if (!draft.reference) {
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3">
          <p className="font-mono text-2xs text-destructive">
            this workload names no weights, so there is nothing to adopt: pick
            an artifact by hand on the next step
          </p>
        </div>
        <ul className="space-y-1">
          {draft.notes.map((note) => (
            <li key={note} className="font-mono text-2xs text-muted-foreground">
              · {note}
            </li>
          ))}
        </ul>
      </div>
    )
  }

  const submit = () => {
    create.mutate(
      {
        digest: draft.digest ?? draft.reference!,
        name: draft.name ?? draft.reference!,
        kind: draft.kind,
        format: draft.format,
        parameter_count: draft.parameter_count,
        verified: draft.verified,
        metadata: draft.metadata,
        locations: draft.source_uri
          ? [
              {
                scheme: draft.source_scheme ?? "hf",
                uri: draft.source_uri,
                preferred: true,
              },
            ]
          : [],
      },
      {
        onSuccess: (artifact: Artifact) => {
          toast.success("Artifact registered", { description: artifact.name })
          onResolved(artifact.id)
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not register these weights")),
      }
    )
  }

  return (
    <div className="min-w-0 space-y-4">
      {draft.existing_id ? (
        <div className="rounded-md border bg-muted/30 p-3">
          <p className="font-mono text-2xs text-muted-foreground">
            the catalog already holds these weights, so there is nothing to
            write
          </p>
        </div>
      ) : (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3">
          <p className="font-mono text-2xs text-warning">
            adopted by reference, not by content: the digest below is the name
            the cluster was given, and the artifact is recorded unverified
          </p>
        </div>
      )}

      <div className="rounded-md border bg-card p-3">
        <Row label="reference" value={draft.reference} />
        <Row label="name" value={<Dash>{draft.name}</Dash>} />
        <Row label="revision" value={<Dash>{draft.revision}</Dash>} />
        <Row label="kind" value={draft.kind} />
        <Row label="format" value={<Dash>{draft.format}</Dash>} />
        <Row
          label="parameters"
          value={
            <Dash>
              {draft.parameter_count &&
                `${(draft.parameter_count / 1_000_000_000).toLocaleString()}B`}
            </Dash>
          }
        />
        <Row label="fetched from" value={<Dash>{draft.source_uri}</Dash>} />
      </div>

      {Object.keys(draft.metadata).length > 0 && (
        <div className="flex flex-wrap gap-1">
          {Object.entries(draft.metadata).map(([k, v]) => (
            <Badge
              key={k}
              variant="outline"
              className="max-w-full font-mono text-2xs break-all"
            >
              {k}={String(v)}
            </Badge>
          ))}
        </div>
      )}

      {draft.notes.length > 0 && (
        <ul className="space-y-1">
          {draft.notes.map((note) => (
            <li key={note} className="font-mono text-2xs text-muted-foreground">
              · {note}
            </li>
          ))}
        </ul>
      )}

      {!draft.existing_id && (
        <Button className="w-full" disabled={create.isPending} onClick={submit}>
          {create.isPending ? "Registering…" : "Register these weights"}
        </Button>
      )}
    </div>
  )
}
