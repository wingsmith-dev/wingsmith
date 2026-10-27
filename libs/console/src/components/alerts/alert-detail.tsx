import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { WriteAction } from "@/components/layout/write-action"
import { ThresholdResolution } from "@/components/alerts/threshold-resolution"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { alertStateTone } from "@/modules/alerts/display"
import { useAcknowledgeAlert, useResolveAlert } from "@/modules/alerts/queries"
import { severityTone } from "@/modules/machinery/display"
import type { Alert, AlertThreshold, MetricRule } from "@/modules/alerts/types"
import { useId, useState } from "react"
import { toast } from "sonner"

/**
 * One alert, read-only, with the number that produced it beside it.
 *
 * Nothing here edits the alert. Acknowledge and resolve are the plane's two
 * lifecycle verbs (`POST` to a sub-path, not a `PATCH` of `state`) and they
 * write the one thing a machine cannot know. The derived fields have no edit
 * affordance and never will.
 */
export function AlertDetail({
  alert,
  thresholds,
  rules,
  open,
  onOpenChange,
  onDeclare,
  onEdit,
}: {
  alert: Alert
  thresholds: AlertThreshold[]
  rules: MetricRule[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onDeclare: (rule: string, subjectUid: string | null) => void
  onEdit: (threshold: AlertThreshold) => void
}) {
  const noteId = useId()
  const [note, setNote] = useState("")
  const acknowledge = useAcknowledgeAlert()
  const resolve = useResolveAlert()

  const details = Object.entries(alert.details)

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle className="pr-8 text-sm leading-snug">
            {alert.title}
          </SheetTitle>
          <SheetDescription className="font-mono text-2xs">
            {alert.id} · {alert.source}
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-5 px-6 pb-6">
          <div className="flex flex-wrap items-center gap-3">
            <StatusLabel
              tone={alertStateTone(alert.state)}
              pulse={alert.state === "firing"}
            >
              {alert.state}
            </StatusLabel>
            <StatusLabel tone={severityTone(alert.severity)}>
              {alert.severity}
            </StatusLabel>
            <Badge variant="outline" className="font-mono text-2xs">
              {alert.rule}
            </Badge>
          </div>

          <FieldGrid>
            <Field label="Subject">
              {alert.subject.type}
              {alert.subject.id && (
                <span className="text-muted-foreground">
                  {" "}
                  · {alert.subject.id}
                </span>
              )}
            </Field>
            <Field label="Cluster">
              <Dash>{alert.cluster_id}</Dash>
            </Field>
            <Field label="First seen">
              <span title={formatAbsolute(alert.first_seen_at)}>
                {formatRelative(alert.first_seen_at)}
              </span>
            </Field>
            <Field label="Last seen">
              <span title={formatAbsolute(alert.last_seen_at)}>
                {formatRelative(alert.last_seen_at)}
              </span>
            </Field>
            <Field label="Fired">
              {alert.fired_at ? (
                <span title={formatAbsolute(alert.fired_at)}>
                  {formatRelative(alert.fired_at)}
                </span>
              ) : (
                <span className="text-muted-foreground">
                  not yet: still pending
                </span>
              )}
            </Field>
            <Field label="Occurrences">{alert.occurrences}</Field>
            <Field label="Acknowledged">
              {alert.acknowledged_at ? (
                <span title={formatAbsolute(alert.acknowledged_at)}>
                  {alert.acknowledged_by ?? "someone"} ·{" "}
                  {formatRelative(alert.acknowledged_at)}
                </span>
              ) : (
                <span className="text-muted-foreground">no</span>
              )}
            </Field>
            <Field label="Notified">
              {alert.notify_error ? (
                <span className="text-destructive">{alert.notify_error}</span>
              ) : (
                <Dash>
                  {alert.notified_at && formatRelative(alert.notified_at)}
                </Dash>
              )}
            </Field>
          </FieldGrid>

          {alert.acknowledged_note && (
            <Field label="Note">{alert.acknowledged_note}</Field>
          )}

          <div className="space-y-2">
            <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
              The number behind it
            </div>
            <ThresholdResolution
              rule={alert.rule}
              subjectUid={alert.subject.id}
              thresholds={thresholds}
              rules={rules}
              onDeclare={() => onDeclare(alert.rule, alert.subject.id)}
              onEdit={onEdit}
            />
          </div>

          {details.length > 0 && (
            <div className="space-y-2">
              <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
                Details
              </div>
              <div className="space-y-1 rounded-md border bg-muted/40 p-3">
                {details.map(([key, value]) => (
                  <div key={key} className="font-mono text-2xs">
                    <span className="text-muted-foreground">{key}</span>{" "}
                    {typeof value === "object" && value !== null
                      ? JSON.stringify(value)
                      : String(value)}
                  </div>
                ))}
              </div>
            </div>
          )}

          {alert.state !== "resolved" && (
            <WriteAction>
              <div className="space-y-2 border-t pt-4">
                <Label htmlFor={noteId}>Acknowledgment note</Label>
                <Input
                  id={noteId}
                  value={note}
                  placeholder="what you are doing about it"
                  onChange={(event) => setNote(event.target.value)}
                  className="text-xs"
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={acknowledge.isPending}
                    onClick={() =>
                      acknowledge.mutate(
                        { ref: alert.id, note: note || null },
                        {
                          onSuccess: () => {
                            setNote("")
                            toast.success("Alert acknowledged")
                          },
                          onError: (error) =>
                            toast.error(
                              formatError(error, "Could not acknowledge")
                            ),
                        }
                      )
                    }
                  >
                    Acknowledge
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={resolve.isPending}
                    onClick={() =>
                      resolve.mutate(alert.id, {
                        onSuccess: () => toast.success("Alert resolved"),
                        onError: (error) =>
                          toast.error(formatError(error, "Could not resolve")),
                      })
                    }
                  >
                    Resolve
                  </Button>
                </div>
                <p className="text-2xs text-muted-foreground">
                  Resolving a condition that is still true is a snooze: the
                  reactor recomputes from scratch and reopens it on the next
                  tick.
                </p>
              </div>
            </WriteAction>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
