import { Field } from "@/components/layout/field"
import { RowActions } from "@/components/layout/destructive"
import { Dash } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { ResourceGlyph } from "@/components/layout/resource-icon"
import { Card } from "@/components/ui/card"
import type { Policy } from "@/modules/binding/types"

/**
 * An empty allow-list means "no opinion", not "nothing allowed", so the two
 * kinds of list are drawn differently and an empty one says which it is.
 */
function List({ values, tone }: { values: string[]; tone: "allow" | "deny" }) {
  if (values.length === 0) {
    return (
      <span className="font-mono text-2xs text-muted-foreground">
        {tone === "allow" ? "no opinion" : "none"}
      </span>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((value) => (
        <Badge
          key={value}
          variant="outline"
          className={
            tone === "deny"
              ? "border-destructive/40 font-mono text-2xs text-destructive"
              : "font-mono text-2xs"
          }
        >
          {value}
        </Badge>
      ))}
    </div>
  )
}

export function PolicyCard({
  policy,
  onEdit,
  onDelete,
}: {
  policy: Policy
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <Card className="h-full gap-0 overflow-hidden p-0">
      <div className="flex flex-wrap items-center gap-3 border-b bg-muted/30 px-4 py-3">
        <ResourceGlyph kind="policy" size="sm" />
        <span className="font-medium">{policy.name}</span>
        <StatusLabel tone={policy.enabled ? "ok" : "neutral"}>
          {policy.enabled ? "enabled" : "disabled"}
        </StatusLabel>
        <div className="ml-auto">
          <RowActions
            actions={[
              { label: "Edit", onSelect: onEdit },
              { label: "Delete", destructive: true, onSelect: onDelete },
            ]}
          />
        </div>
      </div>

      <div className="space-y-4 p-4">
        <p className="text-xs text-muted-foreground">
          <Dash>{policy.description}</Dash>
        </p>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Allowed regions">
            <List values={policy.allowed_regions} tone="allow" />
          </Field>
          <Field label="Residencies">
            <List values={policy.allowed_residencies} tone="allow" />
          </Field>
          <Field label="Device classes">
            <List values={policy.allowed_device_classes} tone="allow" />
          </Field>
          <Field label="Allowed clusters">
            <List values={policy.allowed_clusters} tone="allow" />
          </Field>
          <Field label="Denied clusters" className="sm:col-span-2">
            <List values={policy.denied_clusters} tone="deny" />
          </Field>
          <Field label="Rules">
            {Object.keys(policy.rules).length === 0
              ? "none"
              : `${Object.keys(policy.rules).length} key(s)`}
          </Field>
          <Field label="Attached to" className="sm:col-span-2">
            {policy.attachments.length === 0 ? (
              <span className="text-muted-foreground">
                nothing: constrains nothing
              </span>
            ) : (
              <div className="flex flex-wrap gap-1">
                {policy.attachments.map((attachment) => (
                  <Badge
                    key={attachment.subject_id}
                    variant="secondary"
                    className="font-mono text-2xs"
                  >
                    {attachment.subject_type}:
                    {attachment.subject_name ??
                      attachment.subject ??
                      attachment.subject_id}
                  </Badge>
                ))}
              </div>
            )}
          </Field>
        </div>

        <span className="block font-mono text-2xs text-muted-foreground">
          {policy.id}
        </span>
      </div>
    </Card>
  )
}
