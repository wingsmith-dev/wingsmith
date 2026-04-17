import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { WriteAction } from "@/components/layout/write-action"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatError } from "@/lib/format"
import { useClearClusterServing } from "@/modules/clusters/queries"
import type { Cluster } from "@/modules/clusters/types"
import { useState } from "react"
import { toast } from "sonner"
import { ServingForm } from "./ServingForm"

/**
 * The cluster's data-plane address. Declared, and read by nothing in the control
 * plane — the gateway is the only thing that ever dials it, which is why a
 * cluster with no address here is still a perfectly schedulable cluster.
 */
export function ServingCard({ cluster }: { cluster: Cluster }) {
  const [editing, setEditing] = useState(false)
  const clear = useClearClusterServing()
  const serving = cluster.serving

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          Serving
          {serving && (
            <StatusLabel tone={serving.enabled ? "ok" : "neutral"}>
              {serving.enabled ? "enabled" : "disabled"}
            </StatusLabel>
          )}
        </CardTitle>
        <WriteAction>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => setEditing(true)}
            >
              {serving ? "Edit" : "Set address"}
            </Button>
            {serving && (
              <Button
                size="sm"
                variant="ghost"
                disabled={clear.isPending}
                onClick={() =>
                  clear.mutate(cluster.id, {
                    onSuccess: () => toast.success("Serving address cleared"),
                    onError: (error) =>
                      toast.error(
                        formatError(error, "Could not clear the address")
                      ),
                  })
                }
              >
                Clear
              </Button>
            )}
          </div>
        </WriteAction>
      </CardHeader>
      <CardContent>
        {serving ? (
          <FieldGrid>
            <Field label="URL" className="sm:col-span-2">
              {serving.url}
            </Field>
            <Field label="Credential">
              <Dash>{serving.credential_id}</Dash>
            </Field>
          </FieldGrid>
        ) : (
          <p className="font-mono text-xs text-muted-foreground/60">
            No address. Nothing can route traffic to this cluster until one is
            declared.
          </p>
        )}
      </CardContent>

      {editing && (
        <ServingForm
          cluster={cluster}
          open={editing}
          onOpenChange={setEditing}
        />
      )}
    </Card>
  )
}
