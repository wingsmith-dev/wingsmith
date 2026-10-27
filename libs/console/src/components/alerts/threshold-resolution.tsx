import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import {
  comparisonWord,
  effectiveForSeconds,
  effectiveLookback,
  effectiveSeverity,
  formatSeconds,
  formatThresholdValue,
  resolveThreshold,
} from "@/modules/alerts/display"
import type { AlertThreshold, MetricRule } from "@/modules/alerts/types"

function ScopeBadge({ threshold }: { threshold: AlertThreshold }) {
  return threshold.subject.id === null ? (
    <Badge variant="outline" className="font-mono text-2xs">
      fleet default
    </Badge>
  ) : (
    <Badge variant="secondary" className="font-mono text-2xs">
      {threshold.subject.id}
    </Badge>
  )
}

function Line({
  threshold,
  rule,
}: {
  threshold: AlertThreshold
  rule?: MetricRule
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 font-mono text-2xs text-muted-foreground">
      <ScopeBadge threshold={threshold} />
      <span>
        {comparisonWord(threshold.comparison)}{" "}
        <span className="text-foreground">
          {formatThresholdValue(threshold.value, rule?.unit)}
        </span>
      </span>
      {!threshold.enabled && (
        <Badge variant="destructive" className="font-mono text-2xs">
          disabled
        </Badge>
      )}
    </div>
  )
}

/**
 * Which declared number is actually watching one subject, and what it beat.
 *
 * The whole argument for moving thresholds out of code was that a number could
 * then differ between one subject and the fleet, and be shown beside the alert
 * it produced, neither of which is worth anything if the screen cannot say
 * *which* of the two rows is the one that fired. Inherited and overridden are
 * therefore stated, not implied by a row's position in a table.
 */
export function ThresholdResolution({
  rule,
  subjectUid,
  thresholds,
  rules,
  onDeclare,
  onEdit,
}: {
  rule: string
  subjectUid: string | null
  thresholds: AlertThreshold[]
  rules: MetricRule[]
  onDeclare?: () => void
  onEdit?: (threshold: AlertThreshold) => void
}) {
  const definition = rules.find((entry) => entry.name === rule)
  const { applied, inherited, shadowed, ignored } = resolveThreshold(
    thresholds,
    rule,
    subjectUid
  )

  if (!definition) {
    return (
      <p className="rounded-md border bg-muted/40 px-3 py-2 font-mono text-2xs text-muted-foreground">
        {rule} is a compiled condition, not a metric rule: no declared number is
        behind it.
      </p>
    )
  }

  if (!applied) {
    return (
      <div className="space-y-2 rounded-md border bg-muted/40 px-3 py-2">
        <p className="font-mono text-2xs text-muted-foreground">
          Nothing declares a number for {definition.name}, so the reactor
          evaluates it for nobody. The rule suggests{" "}
          {formatThresholdValue(definition.suggested_value, definition.unit)}.
        </p>
        {onDeclare && (
          <Button size="sm" variant="outline" onClick={onDeclare}>
            Declare a threshold
          </Button>
        )}
      </div>
    )
  }

  const lookback = effectiveLookback(applied, definition)
  const hold = effectiveForSeconds(applied, definition)
  const severity = effectiveSeverity(applied, definition)

  return (
    <div className="space-y-3 rounded-md border bg-muted/40 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs">
            {definition.measures} {comparisonWord(applied.comparison)}{" "}
            {formatThresholdValue(applied.value, definition.unit)}
          </span>
          <Badge
            variant={inherited ? "outline" : "secondary"}
            className="font-mono text-2xs"
          >
            {inherited
              ? "inherited · fleet default"
              : "overridden for this subject"}
          </Badge>
        </div>
        {onEdit && (
          <Button size="sm" variant="outline" onClick={() => onEdit(applied)}>
            {inherited ? "Edit fleet default" : "Edit override"}
          </Button>
        )}
      </div>

      <div className="font-mono text-2xs text-muted-foreground">
        {definition.metric}
      </div>

      <FieldGrid>
        <Field label="Window">
          {lookback.value ?? "—"}
          {lookback.fromRule && (
            <span className="text-muted-foreground"> · from the rule</span>
          )}
        </Field>
        <Field label="Held for">
          {hold.value === null ? "—" : formatSeconds(hold.value)}
          {hold.fromRule && (
            <span className="text-muted-foreground"> · from the rule</span>
          )}
        </Field>
        <Field label="Severity">
          {severity.value ?? "—"}
          {severity.fromRule && (
            <span className="text-muted-foreground"> · from the rule</span>
          )}
        </Field>
        <Field label="Declared because">
          <Dash>{applied.reason}</Dash>
        </Field>
      </FieldGrid>

      {inherited && onDeclare && (
        <Button size="sm" variant="outline" onClick={onDeclare}>
          Override for this subject
        </Button>
      )}

      {shadowed.length > 0 && (
        <div className="space-y-1">
          <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
            Shadowed
          </div>
          {shadowed.map((threshold) => (
            <Line key={threshold.id} threshold={threshold} rule={definition} />
          ))}
        </div>
      )}

      {ignored.length > 0 && (
        <div className="space-y-1">
          <div className="font-mono text-2xs tracking-wider text-warning uppercase">
            Declared but switched off
          </div>
          <p className="font-mono text-2xs text-muted-foreground">
            The reactor loads enabled rows only, so this is not the number that
            fired.
          </p>
          {ignored.map((threshold) => (
            <Line key={threshold.id} threshold={threshold} rule={definition} />
          ))}
        </div>
      )}
    </div>
  )
}
