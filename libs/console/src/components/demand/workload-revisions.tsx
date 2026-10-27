import { DataTable, type Column } from "@/components/layout/data-table"
import { FormDialog, TextareaField } from "@/components/layout/form"
import { Dash } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  useAnnotateWorkloadRevision,
  useUpdateWorkload,
  useWorkloadRevisions,
} from "@/modules/demand/queries"
import {
  changesAgainstPrevious,
  isDeclared,
  liveDigests,
  type ServingChange,
} from "@/modules/demand/revisions"
import type { Workload, WorkloadRevision } from "@/modules/demand/types"
import { IconMessage, IconRotateClockwise } from "@tabler/icons-react"
import { useState } from "react"
import { toast } from "sonner"

/**
 * Every serving configuration a workload has held, and the two things you can do with one.
 *
 * There is no rollback verb on the plane and this table does not invent one. A revision is stored
 * as the patch body that restates it, so "go back to this" is the ordinary `PATCH` every other
 * edit on this page makes — validated on the way in by the same checks, and appending a new
 * revision rather than rewinding to an old one. What that buys is that a configuration the
 * catalog has since dropped fails here, visibly, instead of being restored by a path that never
 * learned the rules.
 *
 * Declared and live are kept apart on purpose. `serving_digest` on the workload is what the plane
 * asked for; the replica counts beside it are what is actually up, per configuration, summed over
 * the clusters. During a roll they disagree, and that interval is the one this table exists to
 * make readable.
 */
export function WorkloadRevisions({ workload }: { workload: Workload }) {
  const list = useWorkloadRevisions(workload.id)
  const [reapplying, setReapplying] = useState<WorkloadRevision | null>(null)
  const [annotating, setAnnotating] = useState<WorkloadRevision | null>(null)
  const live = liveDigests(workload.deployments)
  const items = list.items ?? []

  return (
    <>
      <DataTable
        columns={columns({
          revisions: items,
          workload,
          live,
          onReapply: setReapplying,
          onAnnotate: setAnnotating,
        })}
        list={list}
        rowKey={(revision) => revision.id}
        empty="No serving change has been recorded yet."
        bare
      />
      {reapplying && (
        <ReapplyDialog
          workload={workload}
          revision={reapplying}
          previous={items}
          onClose={() => setReapplying(null)}
        />
      )}
      {annotating && (
        <AnnotateDialog
          workload={workload}
          revision={annotating}
          onClose={() => setAnnotating(null)}
        />
      )}
    </>
  )
}

function columns({
  revisions,
  workload,
  live,
  onReapply,
  onAnnotate,
}: {
  revisions: WorkloadRevision[]
  workload: Workload
  live: Record<string, number>
  onReapply: (revision: WorkloadRevision) => void
  onAnnotate: (revision: WorkloadRevision) => void
}): Column<WorkloadRevision>[] {
  return [
    {
      key: "number",
      header: "Revision",
      cell: (revision) => {
        const declared = isDeclared(revision, workload.serving_digest)
        return (
          <div className="space-y-0.5">
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 font-mono text-2xs",
                declared
                  ? "bg-primary/15 text-primary"
                  : "bg-muted text-muted-foreground"
              )}
            >
              v{revision.number}
            </span>
            <span className="block font-mono text-2xs text-muted-foreground">
              {revision.reason}
            </span>
          </div>
        )
      },
    },
    {
      key: "change",
      header: "Change",
      cell: (revision) => (
        <ChangeList
          changes={changesAgainstPrevious(
            revisions,
            revisions.indexOf(revision)
          )}
          reason={revision.reason}
        />
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (revision) => {
        const declared = isDeclared(revision, workload.serving_digest)
        const ready = live[revision.serving_digest] ?? 0
        return (
          <div className="space-y-0.5">
            <StatusLabel tone={declared ? "ok" : "neutral"}>
              {declared ? "declared" : "superseded"}
            </StatusLabel>
            <span className="block font-mono text-2xs text-muted-foreground">
              {ready > 0 ? `${ready} live` : "nothing running"}
            </span>
          </div>
        )
      },
    },
    {
      key: "note",
      header: "Note",
      cell: (revision) => (
        <span className="line-clamp-2 text-xs text-muted-foreground">
          <Dash>{revision.note}</Dash>
        </span>
      ),
    },
    {
      key: "created",
      header: "Recorded",
      align: "right",
      secondary: true,
      cell: (revision) => (
        <span
          className="font-mono text-2xs text-muted-foreground"
          title={formatAbsolute(revision.created_at)}
        >
          {formatRelative(revision.created_at)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (revision) => (
        <div className="flex items-center justify-end gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Note on revision ${revision.number}`}
            onClick={() => onAnnotate(revision)}
          >
            <IconMessage />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Go back to revision ${revision.number}`}
            disabled={isDeclared(revision, workload.serving_digest)}
            onClick={() => onReapply(revision)}
          >
            <IconRotateClockwise />
          </Button>
        </div>
      ),
    },
  ]
}

const SHOWN_CHANGES = 3

/**
 * What moved, at most a few lines of it.
 *
 * The oldest entry has nothing behind it to diff against, and a `created`, `promoted` or
 * `imported` row says so in words rather than listing every field it declared as a change.
 */
function ChangeList({
  changes,
  reason,
}: {
  changes: ServingChange[]
  reason: string
}) {
  if (!changes.length)
    return (
      <span className="text-xs text-muted-foreground">
        {reason === "imported"
          ? "What this workload was already running"
          : "The configuration it started at"}
      </span>
    )
  const shown = changes.slice(0, SHOWN_CHANGES)
  return (
    <div className="space-y-0.5">
      {shown.map((change) => (
        <div key={change.path} className="font-mono text-2xs">
          <span className="text-muted-foreground">{change.path}</span>{" "}
          <span className="text-muted-foreground">{change.before}</span>
          <span className="px-1 text-muted-foreground">→</span>
          <span>{change.after}</span>
        </div>
      ))}
      {changes.length > shown.length && (
        <div className="text-2xs text-muted-foreground">
          and {changes.length - shown.length} more
        </div>
      )}
    </div>
  )
}

/**
 * Going back, which is an ordinary update carrying an old body.
 *
 * The note defaults to naming the revision because that is the sentence somebody would type
 * anyway, and a history where every entry says why is worth more than one where the reapplies
 * are blank. `revision` on the workload is sent as `If-Match`, the same as every other edit: a
 * configuration change decided against a stale read is exactly what that header is for.
 *
 * The artifact is shown rather than sent. Weights are not patchable, so a revision from before a
 * weights swap restores the configuration and not the model — worth seeing before submitting,
 * and the reason `artifact` sits outside the body at all.
 */
function ReapplyDialog({
  workload,
  revision,
  previous,
  onClose,
}: {
  workload: Workload
  revision: WorkloadRevision
  previous: WorkloadRevision[]
  onClose: () => void
}) {
  const [note, setNote] = useState(`back to v${revision.number}`)
  const update = useUpdateWorkload()
  const current = previous.find((row) =>
    isDeclared(row, workload.serving_digest)
  )
  const changes = current ? changesAgainstPrevious([revision, current], 0) : []
  const differentWeights = revision.artifact !== workload.serving.artifact_id

  return (
    <FormDialog
      icon="workload"
      open
      onOpenChange={(open) => !open && onClose()}
      title={`Go back to revision ${revision.number}`}
      description="Appends a new revision carrying this configuration. Nothing is rewound."
      submitLabel="Apply"
      error={update.error}
      pending={update.isPending}
      onSubmit={() =>
        update.mutate(
          {
            ref: workload.id,
            body: { serving: revision.serving, note: note.trim() || null },
            revision: workload.revision,
          },
          {
            onSuccess: () => {
              onClose()
              toast.success(`${workload.name} is back on v${revision.number}`)
            },
            onError: (error) =>
              toast.error(formatError(error, "Could not apply that revision")),
          }
        )
      }
    >
      <div className="rounded-md border bg-muted/40 px-3 py-2">
        {changes.length ? (
          <ChangeList changes={changes} reason={revision.reason} />
        ) : (
          <span className="text-xs text-muted-foreground">
            This is already the declared configuration.
          </span>
        )}
      </div>

      {differentWeights && (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs">
          This revision served a different artifact ({revision.artifact}). The
          weights are not part of a patch and will not change — only the
          configuration around them.
        </p>
      )}

      <TextareaField
        label="Note"
        value={note}
        onChange={setNote}
        placeholder="Why you are going back"
        hint="Lands on the revision this files. Leave it empty for none."
      />
    </FormDialog>
  )
}

/** The only write a revision takes: why, in somebody's words. */
function AnnotateDialog({
  workload,
  revision,
  onClose,
}: {
  workload: Workload
  revision: WorkloadRevision
  onClose: () => void
}) {
  const [note, setNote] = useState(revision.note ?? "")
  const annotate = useAnnotateWorkloadRevision()

  return (
    <FormDialog
      icon="workload"
      open
      onOpenChange={(open) => !open && onClose()}
      title={`Note on revision ${revision.number}`}
      description="What the configuration was is a record and cannot be edited. Why it changed can."
      error={annotate.error}
      pending={annotate.isPending}
      onSubmit={() =>
        annotate.mutate(
          {
            ref: workload.id,
            revision: String(revision.number),
            body: { note: note.trim() || null },
          },
          {
            onSuccess: () => onClose(),
            onError: (error) =>
              toast.error(formatError(error, "Could not save that note")),
          }
        )
      }
    >
      <TextareaField
        label="Note"
        value={note}
        onChange={setNote}
        rows={4}
        placeholder="Why this configuration"
        hint="Free text. Empty clears it."
      />
    </FormDialog>
  )
}
