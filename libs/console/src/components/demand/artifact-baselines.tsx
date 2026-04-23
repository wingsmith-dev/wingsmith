import { EnforcementBadge } from "@/components/demand/enforcement"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import {
  FormDialog,
  FormGrid,
  NumberField,
  TextField,
} from "@/components/layout/form"
import { ProvenanceFields } from "@/components/demand/provenance"
import { WriteAction } from "@/components/layout/write-action"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatAbsolute, formatError } from "@/lib/format"
import {
  ACCEPTANCE_FLOOR,
  acceptanceFloor,
  formatScore,
} from "@/modules/demand/display"
import {
  useArtifactBaselines,
  useCreateArtifactBaseline,
  useDeleteArtifactBaseline,
  useUpdateArtifactBaseline,
} from "@/modules/demand/queries"
import type { ArtifactBaseline } from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

const FLOOR_NOTE = `A run is graded against ${ACCEPTANCE_FLOOR}x the declared value. tolerance_pct is the recorder's own slack for this dataset and is a different number from that floor — the floor belongs to the criteria version, so raising the standard never rewrites a row here.`

/**
 * The declared quality floors these weights are held to, one row per dataset and
 * metric.
 *
 * Declared, not observed: somebody asserts the number and signs it, so an
 * acceptance review is an argument about the assertion rather than about
 * whichever score a harness produced last. A quantized derivative that regresses
 * is only visible against a baseline written down before the quantization ran.
 */
export function ArtifactBaselines({ artifactId }: { artifactId: string }) {
  const { data: baselines, isLoading } = useArtifactBaselines(artifactId)
  const remove = useDeleteArtifactBaseline(artifactId)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<ArtifactBaseline | undefined>()
  const [doomed, setDoomed] = useState<ArtifactBaseline | undefined>()

  const rows = baselines ?? []

  return (
    <Card className="gap-0 overflow-hidden p-0">
      <CardHeader className="gap-1 border-b bg-muted/30 px-4 py-3">
        <div className="flex items-center gap-2">
          <CardTitle className="text-sm">Baselines ({rows.length})</CardTitle>
          <EnforcementBadge kind="asserted" />
          <WriteAction>
            <Button
              size="sm"
              variant="outline"
              className="ml-auto"
              onClick={() => setCreating(true)}
            >
              New baseline
            </Button>
          </WriteAction>
        </div>
        <p className="text-2xs text-muted-foreground/70">{FLOOR_NOTE}</p>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading && <Skeleton className="m-4 h-20" />}
        {!isLoading && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dataset</TableHead>
                <TableHead>Metric</TableHead>
                <TableHead className="text-right">Declared</TableHead>
                <TableHead className="text-right">
                  Floor ({ACCEPTANCE_FLOOR}x)
                </TableHead>
                <TableHead className="text-right">Tolerance</TableHead>
                <TableHead>Recorded</TableHead>
                <TableHead>Source</TableHead>
                <TableHead className="text-right" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((baseline) => (
                <TableRow key={baseline.id}>
                  <TableCell className="font-mono text-xs">
                    {baseline.dataset}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {baseline.metric}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {formatScore(baseline.value)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {formatScore(acceptanceFloor(baseline.value))}
                  </TableCell>
                  <TableCell className="text-right font-mono text-2xs text-muted-foreground">
                    ±{formatScore(baseline.tolerance_pct)}%
                  </TableCell>
                  <TableCell className="font-mono text-2xs text-muted-foreground">
                    {baseline.recorded_by} ·{" "}
                    {formatAbsolute(baseline.recorded_at)}
                  </TableCell>
                  <TableCell className="max-w-48 truncate font-mono text-2xs text-muted-foreground">
                    {baseline.source_uri ? (
                      <a
                        href={baseline.source_uri}
                        target="_blank"
                        rel="noreferrer"
                        className="underline underline-offset-2"
                      >
                        {baseline.source_uri}
                      </a>
                    ) : (
                      <span className="text-amber-600 dark:text-amber-400">
                        no source
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <RowActions
                      actions={[
                        { label: "Edit", onSelect: () => setEditing(baseline) },
                        {
                          label: "Remove",
                          destructive: true,
                          onSelect: () => setDoomed(baseline),
                        },
                      ]}
                    />
                  </TableCell>
                </TableRow>
              ))}
              {rows.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={8}
                    className="py-10 text-center text-sm text-muted-foreground"
                  >
                    No baseline declared. An accuracy run against these weights
                    grades inconclusive — an absent baseline is never a pass.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        )}
      </CardContent>

      {creating && (
        <BaselineForm
          artifactId={artifactId}
          open
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
      {editing && (
        <BaselineForm
          key={editing.revision}
          artifactId={artifactId}
          baseline={editing}
          open
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="artifact"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="baseline"
        name={`${doomed?.dataset ?? ""} · ${doomed?.metric ?? ""}`}
        note="Runs on that dataset grade inconclusive rather than passing."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Baseline removed", { description: doomed.dataset })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not remove baseline")),
          })
        }
      />
    </Card>
  )
}

/**
 * Dataset and metric are only offered on create: together with the artifact they
 * are the row's identity, so changing one is a different baseline rather than an
 * edit to this one.
 */
export function BaselineForm({
  artifactId,
  baseline,
  open,
  onOpenChange,
}: {
  artifactId: string
  baseline?: ArtifactBaseline
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const editing = !!baseline
  const create = useCreateArtifactBaseline(artifactId)
  const update = useUpdateArtifactBaseline(artifactId)

  const [dataset, setDataset] = useState(baseline?.dataset ?? "")
  const [metric, setMetric] = useState(baseline?.metric ?? "")
  const [value, setValue] = useState<number | null>(baseline?.value ?? null)
  const [tolerance, setTolerance] = useState<number | null>(
    baseline?.tolerance_pct ?? 3
  )
  const [note, setNote] = useState(baseline?.note ?? "")
  const [recordedBy, setRecordedBy] = useState<string | null>(
    baseline?.recorded_by ?? null
  )
  const [recordedAt, setRecordedAt] = useState<string | null>(
    baseline?.recorded_at ?? null
  )
  const [sourceUri, setSourceUri] = useState<string | null>(
    baseline?.source_uri ?? null
  )

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    value: value ?? 0,
    tolerance_pct: tolerance ?? 3,
    source_uri: sourceUri,
    recorded_at: recordedAt,
    note: note || null,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Baseline ${verb}`, { description: `${dataset} · ${metric}` })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        {
          baseline: baseline.id,
          body: { ...shared, recorded_by: recordedBy ?? undefined },
          revision: baseline.revision,
        },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update baseline")),
        }
      )
      return
    }
    create.mutate(
      { dataset, metric, recorded_by: recordedBy ?? "", ...shared },
      {
        onSuccess: () => done("declared"),
        onError: (e) =>
          toast.error(formatError(e, "Could not declare baseline")),
      }
    )
  }

  return (
    <FormDialog
      icon="artifact"
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit baseline" : "New baseline"}
      description="A published score these weights are held to, with where the number came from."
      error={error}
      pending={pending}
      disabled={
        (!editing && (!dataset || !metric)) || value === null || !recordedBy
      }
      onSubmit={submit}
    >
      {!editing && (
        <FormGrid>
          <TextField
            label="Dataset"
            value={dataset}
            onChange={setDataset}
            placeholder="mmlu-pro"
          />
          <TextField
            label="Metric"
            value={metric}
            onChange={setMetric}
            placeholder="accuracy"
          />
        </FormGrid>
      )}

      <FormGrid>
        <NumberField
          label="Declared value"
          value={value}
          onChange={setValue}
          hint={
            value === null
              ? undefined
              : `A run is graded against ${formatScore(acceptanceFloor(value))} — ${ACCEPTANCE_FLOOR}x this.`
          }
        />
        <NumberField
          label="Tolerance (%)"
          value={tolerance}
          onChange={setTolerance}
          min={0}
          hint="This row's own slack. Not the acceptance floor."
        />
      </FormGrid>

      <TextField
        label="Note"
        value={note}
        onChange={setNote}
        mono={false}
        placeholder="0-shot, published table 3"
      />

      <ProvenanceFields
        value={{ by: recordedBy, at: recordedAt, uri: sourceUri }}
        onChange={(provenance) => {
          setRecordedBy(provenance.by)
          setRecordedAt(provenance.at)
          setSourceUri(provenance.uri)
        }}
      />
    </FormDialog>
  )
}
