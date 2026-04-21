import {
  FormDialog,
  FormGrid,
  LabelsField,
  NumberField,
  SelectField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import {
  ALERT_SEVERITIES,
  comparisonWord,
  formatSeconds,
  formatThresholdValue,
} from "@/modules/alerts/display"
import {
  useAlertRules,
  useCreateAlertThreshold,
  useUpdateAlertThreshold,
} from "@/modules/alerts/queries"
import type { AlertThreshold } from "@/modules/alerts/types"
import { useState } from "react"
import { toast } from "sonner"

const FLEET = "__fleet__"
const FROM_RULE = "__rule__"

/**
 * The declared half of alerting: a number, a window and a scope.
 *
 * The rule is picked from `/v1/alert-rules` and never typed, which is what
 * keeps a metric addressed by its registry name — an operator who could write
 * the query could write one that means something else, and the predicate is
 * compiled in either way. Rule and subject are the row's identity under the
 * plane's uniqueness constraint, so they are offered on create only: moving a
 * commitment onto another subject is a different commitment.
 */
export function ThresholdForm({
  open,
  onOpenChange,
  threshold,
  defaultRule,
  defaultSubjectId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  threshold?: AlertThreshold
  defaultRule?: string
  defaultSubjectId?: string | null
}) {
  const editing = !!threshold
  const create = useCreateAlertThreshold()
  const update = useUpdateAlertThreshold()
  const { data: rules } = useAlertRules()

  const [rule, setRule] = useState(threshold?.rule ?? defaultRule ?? "")
  const [scope, setScope] = useState(
    (threshold?.subject.id ?? defaultSubjectId) ? "subject" : FLEET
  )
  const [subjectId, setSubjectId] = useState(
    threshold?.subject.id ?? defaultSubjectId ?? ""
  )
  const [value, setValue] = useState<number | null>(threshold?.value ?? null)
  const [objective, setObjective] = useState(threshold?.objective ?? "")
  const [lookback, setLookback] = useState(threshold?.lookback ?? "")
  const [forSeconds, setForSeconds] = useState<number | null>(
    threshold?.for_seconds ?? null
  )
  const [severity, setSeverity] = useState(threshold?.severity ?? FROM_RULE)
  const [dimensions, setDimensions] = useState<Record<string, string>>(
    threshold?.dimensions ?? {}
  )
  const [enabled, setEnabled] = useState(threshold?.enabled ?? true)
  const [reason, setReason] = useState(threshold?.reason ?? "")

  const definition = (rules ?? []).find((entry) => entry.name === rule)
  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const pickRule = (next: string) => {
    setRule(next)
    const picked = (rules ?? []).find((entry) => entry.name === next)
    if (picked && value === null) setValue(picked.suggested_value)
  }

  const shared = {
    value: value ?? 0,
    dimensions,
    objective: objective || null,
    lookback: lookback || null,
    for_seconds: forSeconds,
    severity: severity === FROM_RULE ? null : severity,
    enabled,
    reason: reason || null,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Threshold ${verb}`, { description: rule })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        { ref: threshold.id, body: shared },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update threshold")),
        }
      )
      return
    }
    create.mutate(
      {
        rule,
        subject_id: scope === FLEET ? null : subjectId,
        ...shared,
      },
      {
        onSuccess: () => done("declared"),
        onError: (e) =>
          toast.error(formatError(e, "Could not declare threshold")),
      }
    )
  }

  const valueHint = definition
    ? `Fires ${comparisonWord(definition.comparison)} this, measured in ${definition.unit || "the rule's unit"}. The rule suggests ${formatThresholdValue(definition.suggested_value, definition.unit)}.`
    : "Pick a rule first — it decides the unit."

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit threshold" : "New threshold"}
      description="A number this plane commits to, and the rule that watches it."
      error={error}
      pending={pending}
      disabled={
        !rule ||
        value === null ||
        (!editing && scope !== FLEET && !subjectId.trim())
      }
      onSubmit={submit}
      wide
    >
      {!editing && (
        <FormGrid>
          <SelectField
            label="Rule"
            value={rule}
            onChange={pickRule}
            placeholder="Pick a rule…"
            hint={
              definition
                ? `${definition.measures} · ${definition.metric}`
                : "Rules are compiled in; the number is what you declare."
            }
            options={(rules ?? []).map((entry) => ({
              value: entry.name,
              label: `${entry.name} — ${entry.metric}`,
            }))}
          />
          <SelectField
            label="Scope"
            value={scope}
            onChange={setScope}
            hint="A fleet default covers every subject of the rule's type until one overrides it."
            options={[
              { value: FLEET, label: "fleet default" },
              {
                value: "subject",
                label: `one ${definition?.subject_type ?? "subject"}`,
              },
            ]}
          />
          {scope !== FLEET && (
            <TextField
              label={`${definition?.subject_type ?? "Subject"} id`}
              value={subjectId}
              onChange={setSubjectId}
              placeholder="dep_01j9x2…"
              hint="The id of the object this number is about."
            />
          )}
        </FormGrid>
      )}

      <FormGrid>
        <NumberField
          label="Value"
          value={value}
          onChange={setValue}
          hint={valueHint}
        />
        <TextField
          label="Objective"
          value={objective}
          onChange={setObjective}
          placeholder="p99 < 2s"
          hint="What this number is in service of. Disclosure, never evaluated."
        />
        <TextField
          label="Window"
          value={lookback}
          onChange={setLookback}
          placeholder={definition?.lookback ?? "5m"}
          hint={
            definition
              ? `Blank inherits the rule's ${definition.lookback}.`
              : "Blank inherits the rule's window."
          }
        />
        <NumberField
          label="Held for (seconds)"
          value={forSeconds}
          onChange={setForSeconds}
          min={0}
          hint={
            definition
              ? `Blank inherits the rule's ${formatSeconds(definition.for_seconds)}. Zero fires on the first tick.`
              : "Blank inherits the rule's hold."
          }
        />
        <SelectField
          label="Severity"
          value={severity}
          onChange={setSeverity}
          hint={
            definition
              ? `The rule's own is ${definition.severity}.`
              : "Blank keeps the rule's own severity."
          }
          options={[
            { value: FROM_RULE, label: "inherit from the rule" },
            ...ALERT_SEVERITIES.map((s) => ({ value: s, label: s })),
          ]}
        />
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          mono={false}
          placeholder="why this number and not another"
          hint="Read at 3am by whoever did not write it."
        />
      </FormGrid>

      <LabelsField
        label="Dimensions"
        value={dimensions}
        onChange={setDimensions}
      />

      <SwitchField
        label="Enabled"
        hint="Off keeps the commitment on record and takes it out of evaluation — the fleet default then applies."
        checked={enabled}
        onChange={setEnabled}
      />
    </FormDialog>
  )
}
