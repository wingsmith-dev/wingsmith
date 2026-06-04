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
import { useServingDraft } from "@/modules/discovered/queries"
import type {
  DiscoveredWorkload,
  ServingDraft,
} from "@/modules/discovered/types"
import { ArtifactStep } from "./artifact-step"
import { PromoteDialog } from "./promote-dialog"
import { useState } from "react"

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <span className="font-mono text-2xs text-muted-foreground/60">
        {label}
      </span>
      <span className="min-w-0 text-right font-mono text-xs break-all">
        {value}
      </span>
    </div>
  )
}

function Read({ draft }: { draft: ServingDraft }) {
  return (
    <div className="min-w-0 space-y-4">
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
              {draft.engine &&
                [
                  draft.engine,
                  draft.engine_runtime_version &&
                    `runtime ${draft.engine_runtime_version}`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
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
              <Badge
                key={k}
                variant="outline"
                className="max-w-full font-mono text-2xs break-all"
              >
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
                className="font-mono text-2xs break-all text-muted-foreground"
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

/** The steps of adoption, in the order the plane can take them. */
const STEPS = [
  { key: "weights", label: "weights" },
  { key: "serving", label: "serving" },
  { key: "deployment", label: "deployment" },
] as const

type Step = (typeof STEPS)[number]["key"]

function Steps({ current }: { current: Step }) {
  const at = STEPS.findIndex((step) => step.key === current)
  return (
    <div className="flex items-center gap-1.5 font-mono text-2xs">
      {STEPS.map((step, i) => (
        <span key={step.key} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-muted-foreground/40">→</span>}
          <span
            className={
              i === at
                ? "text-foreground"
                : i < at
                  ? "text-muted-foreground"
                  : "text-muted-foreground/40"
            }
          >
            {i < at ? `${step.label} ✓` : step.label}
          </span>
        </span>
      ))}
    </div>
  )
}

/**
 * What the plane reads a running workload as, and the path from that reading to
 * something it manages.
 *
 * The reading is shown rather than applied at every step: a draft is assembled
 * from a container's arguments and an image tag, which is someone else's intent
 * as we understood it and not a statement of ours. So each step shows what it is
 * about to write before it writes it.
 *
 * Two objects rather than three, since the serving configuration stopped being
 * one: the weights are registered, and then the adoption carries the serving
 * block inline the way a create does.
 *
 * The first step is why this exists. A serving block needs an artifact, a
 * workload we never declared has none, and the form used to dead-end asking for
 * one that nobody had created.
 */
export function ServingDraftDialog({
  workload,
  trigger,
}: {
  workload: DiscoveredWorkload
  trigger?: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [step, setStep] = useState<Step>("weights")
  const [registered, setArtifactId] = useState("")
  const [promoting, setPromoting] = useState(false)
  const { data: draft, isLoading, error } = useServingDraft(workload.id, open)

  /** Whatever this run registered, or what the catalogue already held. */
  const artifactId = registered || draft?.artifact.existing_id || ""

  const reset = () => {
    setStep("weights")
    setArtifactId("")
  }

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          setOpen(next)
          if (!next && !promoting) reset()
        }}
      >
        <DialogTrigger asChild>
          {trigger ?? (
            <Button size="sm" variant="outline">
              Read as serving
            </Button>
          )}
        </DialogTrigger>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="break-all">
              {workloadLabel(workload)}
            </DialogTitle>
            <DialogDescription>
              How the plane reads what this cluster is running. Nothing has been
              written — this is a proposal to confirm, not a change.
            </DialogDescription>
          </DialogHeader>

          <Steps current={step} />

          {isLoading && (
            <p className="font-mono text-2xs text-muted-foreground">reading…</p>
          )}
          {error && (
            <p className="font-mono text-2xs text-destructive">
              {formatError(error, "Could not read this workload")}
            </p>
          )}

          {draft && step === "weights" && (
            <ArtifactStep
              draft={draft.artifact}
              onResolved={(id) => {
                setArtifactId(id)
                setStep("serving")
              }}
            />
          )}
          {draft && step === "serving" && (
            <>
              <Read draft={draft} />
              <p className="font-mono text-2xs text-muted-foreground/60">
                Nothing here is submitted. The plane derives the block it adopts
                from this same reading, so what is declared is what is already
                running — changing any of it is a PATCH afterwards, where a
                restart is what you asked for.
              </p>
            </>
          )}
          {draft && step === "deployment" && (
            <div className="rounded-md border bg-muted/30 p-3">
              <p className="font-mono text-2xs text-muted-foreground">
                the weights exist and the serving block is read. Adopting takes
                the workload over in place — it keeps the replicas it already
                has, and the cluster refuses to reconcile it if what is running
                stops matching what was adopted.
              </p>
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Close
            </Button>
            {step === "weights" && draft?.artifact.existing_id && (
              <Button onClick={() => setStep("serving")}>
                Continue to serving
              </Button>
            )}
            {step === "weights" && draft && !draft.artifact.reference && (
              <Button onClick={() => setStep("serving")}>Skip</Button>
            )}
            {step === "serving" && (
              <Button
                disabled={!artifactId}
                onClick={() => setStep("deployment")}
              >
                Use this configuration
              </Button>
            )}
            {step === "deployment" && (
              <Button
                onClick={() => {
                  setPromoting(true)
                  setOpen(false)
                }}
              >
                Adopt as a deployment
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <PromoteDialog
        workload={workload}
        open={promoting}
        artifact={artifactId}
        onOpenChange={(next) => {
          setPromoting(next)
          if (!next) {
            reset()
          }
        }}
      />
    </>
  )
}
