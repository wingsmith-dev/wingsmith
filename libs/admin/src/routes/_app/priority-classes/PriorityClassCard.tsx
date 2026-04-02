import { Field } from "@/components/layout/field"
import { RowActions } from "@/components/layout/destructive"
import { Badge } from "@/components/ui/badge"
import { ResourceGlyph } from "@/components/layout/resource-icon"
import { Card } from "@/components/ui/card"
import { Dash } from "@/components/layout/mono"
import { formatRelative } from "@/lib/format"
import type { PriorityClass } from "@/modules/binding/types"

/**
 * `preempts` and `preemptible` sit side by side rather than folding into one
 * axis: a class can take capacity from others and still be safe from eviction
 * itself, and that pairing is the whole content of the object.
 */
export function PriorityClassCard({
  priorityClass,
  onEdit,
  onDelete,
}: {
  priorityClass: PriorityClass
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <Card className="h-full overflow-hidden p-0">
      <div className="flex flex-wrap items-center gap-3 border-b bg-muted/30 px-4 py-3">
        <ResourceGlyph kind="priority-class" size="sm" />
        <span className="font-medium">{priorityClass.name}</span>
        <span className="font-mono text-2xs text-muted-foreground/60">
          {priorityClass.value}
        </span>
        <div className="ml-auto flex items-center gap-2">
          {priorityClass.is_default && (
            <Badge variant="secondary" className="font-mono text-2xs">
              default
            </Badge>
          )}
          <RowActions
            actions={[
              { label: "Edit", onSelect: onEdit },
              { label: "Delete", destructive: true, onSelect: onDelete },
            ]}
          />
        </div>
      </div>

      <div className="space-y-4 p-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Preempts others">
            {priorityClass.preempts ? "yes" : "no"}
          </Field>
          <Field label="Can be preempted">
            {priorityClass.preemptible ? "yes" : "no"}
          </Field>
          <Field label="Value">{priorityClass.value}</Field>
          <Field label="Updated">
            {formatRelative(priorityClass.updated_at)}
          </Field>
        </div>

        <Field label="Description">
          <span className="text-muted-foreground">
            <Dash>{priorityClass.description}</Dash>
          </span>
        </Field>

        <span className="block font-mono text-2xs text-muted-foreground/50">
          {priorityClass.id}
        </span>
      </div>
    </Card>
  )
}
