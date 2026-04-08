import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import { Dash } from "@/components/layout/mono"
import { formatError } from "@/lib/format"
import { workloadLabel } from "@/modules/discovered/display"
import { useRecipeDraft } from "@/modules/discovered/queries"
import type {
  DiscoveredWorkload,
  RecipeDraft,
} from "@/modules/discovered/types"
import { useState } from "react"

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <span className="font-mono text-2xs text-muted-foreground/60">
        {label}
      </span>
      <span className="text-right font-mono text-xs">{value}</span>
    </div>
  )
}

function Read({ draft }: { draft: RecipeDraft }) {
  return (
    <div className="space-y-4">
      {!draft.confident && (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3">
          <p className="font-mono text-2xs text-amber-600 dark:text-amber-400">
            this could not be read with confidence — check every field before
            saving
          </p>
        </div>
      )}

      <div className="rounded-md border bg-card p-3">
        <Row
          label="engine"
          value={
            <Dash>
              {draft.engine && `${draft.engine} ${draft.engine_version ?? ""}`}
            </Dash>
          }
        />
        <Row label="weights" value={<Dash>{draft.artifact_name}</Dash>} />
        <Row
          label="context length"
          value={<Dash>{draft.context_length?.toLocaleString()}</Dash>}
        />
        <Row
          label="parallelism"
          value={`tp=${draft.tensor_parallel} pp=${draft.pipeline_parallel} ep=${draft.expert_parallel}`}
        />
        <Row
          label="devices"
          value={`${draft.device_count} × ${draft.device_class ?? "unknown class"}`}
        />
        <Row
          label="kv cache"
          value={<Dash>{draft.kv_cache_fraction?.toString()}</Dash>}
        />
        <Row label="quantization" value={<Dash>{draft.quantization}</Dash>} />
        <Row
          label="cpu / memory"
          value={
            <Dash>
              {draft.cpu_cores &&
                `${draft.cpu_cores} cores / ${Math.round((draft.memory_mib ?? 0) / 1024)} GiB`}
            </Dash>
          }
        />
      </div>

      {Object.keys(draft.engine_args).length > 0 && (
        <div>
          <p className="mb-1.5 font-mono text-2xs text-muted-foreground/60">
            carried through as engine args
          </p>
          <div className="flex flex-wrap gap-1">
            {Object.entries(draft.engine_args).map(([k, v]) => (
              <Badge key={k} variant="outline" className="font-mono text-2xs">
                {k}
                {v !== "" && `=${v}`}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {draft.notes.length > 0 && (
        <div>
          <p className="mb-1.5 font-mono text-2xs text-muted-foreground/60">
            what could not be determined
          </p>
          <ul className="space-y-1">
            {draft.notes.map((note) => (
              <li
                key={note}
                className="font-mono text-2xs text-muted-foreground"
              >
                — {note}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/**
 * What the plane reads a running workload as, before anything is written.
 *
 * Shown rather than applied: the draft is assembled from a container's arguments
 * and an image tag, which is a reading of someone else's intent and not a
 * statement of ours. A person confirms it, and only then does a recipe exist.
 */
export function RecipeDraftDialog({
  workload,
  trigger,
}: {
  workload: DiscoveredWorkload
  trigger?: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const { data: draft, isLoading, error } = useRecipeDraft(workload.id, open)

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" variant="outline">
            Read as recipe
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{workloadLabel(workload)}</DialogTitle>
          <DialogDescription>
            How the plane reads what this cluster is running. Nothing has been
            written — this is a proposal to confirm, not a change.
          </DialogDescription>
        </DialogHeader>

        {isLoading && (
          <p className="font-mono text-2xs text-muted-foreground">reading…</p>
        )}
        {error && (
          <p className="font-mono text-2xs text-destructive">
            {formatError(error, "Could not read this workload")}
          </p>
        )}
        {draft && <Read draft={draft} />}

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
